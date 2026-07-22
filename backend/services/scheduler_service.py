"""
定时任务调度服务
"""

import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Callable
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy.orm import Session


from database import SessionLocal
from models import BookingTask, BookingAccount, TaskLog, SystemConfig
from services.email_service import KukuMailService
from services.hku_api import HKUApiService

def _get_recaptcha_token_safe():
    """安全获取 reCAPTCHA 令牌（失败时返回 None）"""
    try:
        from services.hku_browser_service import get_recaptcha_token
        return get_recaptcha_token()
    except Exception:
        return None



logger = logging.getLogger(__name__)


class BookingScheduler:
    def __init__(self):
        self.scheduler = BackgroundScheduler()
        self.running_tasks = {}  # task_id -> thread
        self.stop_flags = {}  # task_id -> bool
    
    def start(self):
        """启动调度器"""
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("调度器已启动")
    
    def stop(self):
        """停止调度器"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("调度器已停止")
    
    def add_log(self, db: Session, task_id: int, level: str, message: str):
        """添加任务日志"""
        log = TaskLog(task_id=task_id, level=level, message=message)
        db.add(log)
        db.commit()
    
    def get_global_kuku_config(self, db: Session) -> dict:
        """获取全局邮箱配置"""
        cookie = db.query(SystemConfig).filter(SystemConfig.key == "kuku_cookie").first()
        token = db.query(SystemConfig).filter(SystemConfig.key == "kuku_token").first()
        subtoken = db.query(SystemConfig).filter(SystemConfig.key == "kuku_subtoken").first()
        return {
            "cookie": cookie.value if cookie else None,
            "token": token.value if token else None,
            "subtoken": subtoken.value if subtoken else None
        }
    
    def auto_login_and_get_token(
        self, 
        db: Session,
        account: BookingAccount,
        task_id: int
    ) -> Optional[str]:
        """自动登录获取Token（使用全局邮箱配置）"""
        config = self.get_global_kuku_config(db)
        if not config["cookie"] or not config["token"] or not config["subtoken"]:
            self.add_log(db, task_id, "ERROR", "邮箱配置不完整，请在系统设置中配置 Token、SubToken 和 Cookie")
            return None
        
        try:
            # 1. 创建临时邮箱
            mail_service = KukuMailService(
                token=config["token"],
                subtoken=config["subtoken"],
                cookie=config["cookie"]
            )
            
            self.add_log(db, task_id, "INFO", "正在创建临时邮箱...")
            temp_email = mail_service.create_email()
            
            if not temp_email:
                self.add_log(db, task_id, "ERROR", "创建临时邮箱失败")
                return None
            
            # 更新账号的临时邮箱
            account.temp_email = temp_email
            account.email = temp_email
            db.commit()
            
            self.add_log(db, task_id, "INFO", f"临时邮箱: {temp_email}")
            
            # 2. 通过浏览器登录（自动处理 reCAPTCHA + 验证码 + 登录）
            from services.hku_browser_service import BrowserLoginService
            browser = BrowserLoginService(mail_service)
            success, msg, token = browser.auto_login(temp_email)
            
            if not success or not token:
                self.add_log(db, task_id, "ERROR", f"登录失败: {msg}")
                return None
            
            # 更新账号Token
            account.hku_token = token
            account.token_status = "已登录"
            db.commit()
            
            self.add_log(db, task_id, "INFO", "登录成功，Token已更新")
            return token
            
        except Exception as e:
            self.add_log(db, task_id, "ERROR", f"自动登录异常: {str(e)}")
            return None
    
    def execute_booking_task(self, task_id: int):
        """执行预约任务（根据 task_mode 分发到不同执行逻辑）"""
        db = SessionLocal()
        
        try:
            task = db.query(BookingTask).filter(BookingTask.id == task_id).first()
            if not task:
                logger.error(f"任务不存在: {task_id}")
                return
            
            account = db.query(BookingAccount).filter(BookingAccount.id == task.account_id).first()
            if not account:
                self.add_log(db, task_id, "ERROR", "关联账号不存在")
                task.status = "failed"
                task.result_message = "关联账号不存在"
                db.commit()
                return
            
            # 更新状态
            task.status = "running"
            task.executed_at = datetime.utcnow()
            db.commit()
            
            mode_label = "极速模式" if task.task_mode == "rapid" else "稳定模式"
            self.add_log(db, task_id, "INFO", f"[{mode_label}] 开始执行任务: {task.target_date} {task.time_slot}")
            
            if task.task_mode == "rapid":
                self._execute_rapid_mode(db, task, account)
            else:
                self._execute_stable_mode(db, task, account)
            
        except Exception as e:
            logger.exception(f"任务执行异常: {task_id}")
            try:
                task = db.query(BookingTask).filter(BookingTask.id == task_id).first()
                if task:
                    task.status = "failed"
                    task.result_message = f"执行异常: {str(e)}"
                    db.commit()
                self.add_log(db, task_id, "ERROR", f"执行异常: {str(e)}")
            except:
                pass
        finally:
            db.close()
            # 清理
            if task_id in self.running_tasks:
                del self.running_tasks[task_id]
            if task_id in self.stop_flags:
                del self.stop_flags[task_id]
    
    def _ensure_token(self, db: Session, task: BookingTask, account: BookingAccount) -> Optional[str]:
        """确保账号有有效Token，返回token或None"""
        token = account.hku_token
        if not token and task.auto_login:
            self.add_log(db, task.id, "INFO", "Token为空，尝试自动登录...")
            token = self.auto_login_and_get_token(db, account, task.id)
        
        if not token:
            task.status = "failed"
            task.result_message = "无有效Token"
            db.commit()
            return None
        
        return token
    
    def _execute_rapid_mode(self, db: Session, task: BookingTask, account: BookingAccount):
        """
        极速模式：与桌面版 app.py 完全一致的逻辑
        - 跳过Token验证（节省一次网络请求）
        - 跳过slot查询（节省一次网络请求）
        - 直接用预设slot_id高速连续打
        """
        task_id = task.id
        
        # 获取Token（不验证，节省时间）
        token = self._ensure_token(db, task, account)
        if not token:
            return
        
        # 极速模式必须有 slot_id
        slot_id = task.slot_id
        if not slot_id:
            # 如果没有预设slot_id，根据时段计算默认值
            self.add_log(db, task_id, "WARN", "极速模式未指定Slot ID，尝试快速查询...")
            hku_api = HKUApiService(token)
            slot = hku_api.find_slot(task.target_date, task.time_slot)
            if slot:
                slot_id = slot.get("id")
                task.slot_id = slot_id
                db.commit()
                self.add_log(db, task_id, "INFO", f"查询到 Slot ID: {slot_id}")
            else:
                task.status = "failed"
                task.result_message = "极速模式需要指定Slot ID，且自动查询失败"
                db.commit()
                return
        
        self.add_log(db, task_id, "INFO", f"极速模式: 直接使用 Slot ID={slot_id}，跳过Token验证，开始单次请求（不重试）")
        
        hku_api = HKUApiService(token)
        
        # 极速模式：只执行一次，不重试
        success = False
        if self.stop_flags.get(task_id, False):
            self.add_log(db, task_id, "INFO", "任务被手动停止")
            task.status = "cancelled"
            db.commit()
            return
        
        self.add_log(db, task_id, "INFO", "极速请求（单次，不重试）...")
        
        recaptcha_token = _get_recaptcha_token_safe()
        ok, result_msg = hku_api.book(
            name=account.name,
            id_card=account.id_card,
            target_date=task.target_date,
            slot_id=slot_id,
            companions=account.companions,
            entourage_list=account.entourage_list or [],
            recaptcha_token=recaptcha_token
        )
        
        if ok:
            # 检查是否是重复预约
            if "已有预约记录" in result_msg or "repeated" in result_msg.lower():
                # 重复预约视为失败，不继续验证
                task.status = "failed"
                task.result_message = "已有预约记录（重复预约）"
                if account.is_auto_created:
                    account.booking_success_time = datetime.utcnow()
                    self.add_log(db, task_id, "WARN", f"账号 {account.id} 重复预约，已标记为不再使用")
                db.commit()
                return  # 直接返回，不进行验证
            
            success = True
            task.result_message = result_msg  # 保存结果消息
            self.add_log(db, task_id, "INFO", f"预约请求成功: {result_msg}")
        else:
            # 预约失败，直接标记为失败，不重试
            task.status = "failed"
            task.result_message = f"预约失败: {result_msg}"
            self.add_log(db, task_id, "WARN", f"预约失败: {result_msg}（极速模式不重试）")
            db.commit()
            return  # 直接返回，不进行验证
        
        # 验证预约结果
        self._verify_and_finish(db, task, hku_api, success)
    
    def _execute_stable_mode(self, db: Session, task: BookingTask, account: BookingAccount):
        """
        稳定模式：先验证Token，再查询slot，然后预约
        """
        task_id = task.id
        
        # 获取Token
        token = self._ensure_token(db, task, account)
        if not token:
            return
        
        # 验证Token
        hku_api = HKUApiService(token)
        valid, msg = hku_api.verify_token()
        
        if not valid:
            if task.auto_login:
                self.add_log(db, task_id, "WARN", "Token已失效，尝试重新登录...")
                token = self.auto_login_and_get_token(db, account, task_id)
                if not token:
                    task.status = "failed"
                    task.result_message = "Token失效且重新登录失败"
                    db.commit()
                    return
                hku_api = HKUApiService(token)
            else:
                task.status = "failed"
                task.result_message = "Token已失效"
                db.commit()
                return
        
        # 查找目标slot
        slot = hku_api.find_slot(task.target_date, task.time_slot)
        
        if not slot:
            self.add_log(db, task_id, "WARN", "未找到目标时段，尝试直接预约...")
            if task.slot_id:
                slot_id = task.slot_id
            else:
                task.status = "failed"
                task.result_message = "未找到可用时段"
                db.commit()
                return
        else:
            slot_id = slot.get("id")
            # 使用计算后的实际剩余名额
            available_quota = slot.get("available_quota", slot.get("quota", 0))
            original_quota = slot.get("original_quota", slot.get("quota", 0))
            booked_amount = slot.get("booked_amount", 0)
            
            # 检查是否有可用名额
            if available_quota <= 0:
                task.status = "failed"
                task.result_message = f"该时段已无可用名额 (总名额: {original_quota}, 已预约: {booked_amount}, 剩余: {available_quota})"
                db.commit()
                self.add_log(db, task_id, "WARN", task.result_message)
                return
            
            task.slot_id = slot_id
            db.commit()
            self.add_log(db, task_id, "INFO", f"找到时段 ID: {slot_id}, 剩余名额: {available_quota} (总名额: {original_quota}, 已预约: {booked_amount})")
        
        # 执行预约（高速重试）
        success = False
        for retry in range(task.max_retries):
            if self.stop_flags.get(task_id, False):
                self.add_log(db, task_id, "INFO", "任务被手动停止")
                task.status = "cancelled"
                db.commit()
                return
            
            self.add_log(db, task_id, "INFO", f"尝试预约 ({retry + 1}/{task.max_retries})...")
            
            recaptcha_token = _get_recaptcha_token_safe()
            ok, result_msg = hku_api.book(
                name=account.name,
                id_card=account.id_card,
                target_date=task.target_date,
                slot_id=slot_id,
                companions=account.companions,
                entourage_list=account.entourage_list or [],
                recaptcha_token=recaptcha_token
            )
            
            if ok:
                # 检查是否是重复预约
                if "已有预约记录" in result_msg or "repeated" in result_msg.lower():
                    # 重复预约视为失败，不继续验证
                    task.status = "failed"
                    task.result_message = "已有预约记录（重复预约）"
                    if account.is_auto_created:
                        account.booking_success_time = datetime.utcnow()
                        self.add_log(db, task_id, "WARN", f"账号 {account.id} 重复预约，已标记为不再使用")
                    db.commit()
                    return  # 直接返回，不进行验证
                
                success = True
                task.result_message = result_msg  # 保存结果消息
                self.add_log(db, task_id, "INFO", f"预约请求成功: {result_msg}")
                break
            else:
                self.add_log(db, task_id, "WARN", f"预约失败: {result_msg}")
                if "登录" in result_msg or "token" in result_msg.lower():
                    if task.auto_login:
                        token = self.auto_login_and_get_token(db, account, task_id)
                        if token:
                            hku_api = HKUApiService(token)
            # 不加 sleep，高速重试
        
        # 验证预约结果
        self._verify_and_finish(db, task, hku_api, success)

    def _verify_and_finish(self, db: Session, task: BookingTask, hku_api: HKUApiService, success: bool):
        """验证预约结果并更新任务状态"""
        # 1. 快速失败处理
        if not success:
            task.status = "failed"
            task.result_message = "达到最大重试次数"
            db.commit()
            return

        account = db.query(BookingAccount).filter(BookingAccount.id == task.account_id).first()
        self.add_log(db, task.id, "INFO", "正在验证预约结果...")

        # 2. 调用 API 验证
        verified, verify_msg = hku_api.verify_appointment(task.target_date)

        if verified:
            # 情况 A: 验证成功
            task.status = "success"
            task.result_message = verify_msg
            self.add_log(db, task.id, "INFO", f"预约确认成功: {verify_msg}")

            if account:
                account.booking_success_time = datetime.utcnow()
                self.add_log(db, task.id, "INFO", f"已记录账号 {account.id} 的预约成功时间")

            # ★ 获取并验证确认邮件 ★
            email_verified = self._fetch_confirmation_email(db, task)
            
            # 如果邮件验证失败，记录警告但不改变任务状态（因为API验证已成功）
            if not email_verified:
                self.add_log(db, task.id, "WARN", "⚠️ 警告：API验证成功但未找到对应的预约成功邮件，请手动核对")

        else:
            # 情况 B: 验证失败，进一步细分原因
            is_repeated = "已有预约记录" in verify_msg or "repeated" in verify_msg.lower()

            if is_repeated:
                task.status = "failed"
                task.result_message = "已有预约记录（重复预约）"
                # 即使是重复预约，对于自动账号也视为已消耗
                if account and account.is_auto_created:
                    account.booking_success_time = datetime.utcnow()
                    self.add_log(db, task.id, "WARN", f"账号 {account.id} 重复预约，标记为失效")
            else:
                task.status = "failed"
                task.result_message = f"预约提交成功但验证失败: {verify_msg}"
                self.add_log(db, task.id, "WARN", task.result_message)

        # 3. 统一提交
        db.commit()
    
    def _fetch_confirmation_email(self, db: Session, task: BookingTask):
        """
        在临时邮箱中查找预约成功确认邮件。
        通过轮询最近邮件列表、逐封拉取正文并校验，避免旧版 wait_for_email 只取 HTML 中
        第一封邮件而误存登录验证码邮件的问题。仅校验通过才写入 confirmation_email。
        """
        try:
            account = db.query(BookingAccount).filter(BookingAccount.id == task.account_id).first()
            if not account or not account.temp_email:
                self.add_log(db, task.id, "WARN", "无法获取确认邮件：账号未设置临时邮箱")
                return False

            config = self.get_global_kuku_config(db)
            if not config["cookie"] or not config["token"] or not config["subtoken"]:
                self.add_log(db, task.id, "WARN", "无法获取确认邮件：邮箱配置不完整")
                return False

            mail_service = KukuMailService(
                token=config["token"],
                subtoken=config["subtoken"],
                cookie=config["cookie"]
            )

            self.add_log(db, task.id, "INFO", "正在从邮箱列表中扫描预约成功确认邮件（最多约 3 分钟）...")

            deadline = time.time() + 180
            tried_mail_ids: set[str] = set()
            poll_interval = 3

            while time.time() < deadline:
                recent = mail_service.get_recent_emails(account.temp_email, limit=40)
                for item in recent:
                    mid = str(item.get("id") or "")
                    mkey = item.get("key") or ""
                    if not mid or not mkey or mid in tried_mail_ids:
                        continue

                    email_content = mail_service.get_email_content(mid, mkey)
                    if not email_content:
                        continue

                    tried_mail_ids.add(mid)

                    if self._verify_success_email(email_content, task.target_date):
                        task.confirmation_email = email_content
                        self.add_log(db, task.id, "INFO", "确认邮件已获取并保存（已验证为预约成功邮件）")
                        db.commit()
                        return True

                time.sleep(poll_interval)

            self.add_log(db, task.id, "WARN", "等待确认邮件超时：未在邮件列表中匹配到含目标日期的预约成功正文")
            return False

        except Exception as e:
            logger.error(f"获取确认邮件异常: {e}")
            self.add_log(db, task.id, "WARN", f"获取确认邮件失败: {str(e)}")
            return False

    def _verify_success_email(self, email_content: str, target_date: str) -> bool:
        """验证邮件内容是否为港大参观预约成功通知（需同时满足成功特征与目标日期）"""
        if not email_content:
            return False

        email_lower = email_content.lower()

        # 明显为登录验证码类邮件，直接排除（避免误匹配）
        login_only_markers = [
            "verification code for logging in the tourist registration system",
            "登錄驗證碼",
            "登录验证码",
            "驗證碼用於登錄",
        ]
        if any(m.lower() in email_lower for m in login_only_markers):
            if not any(
                k in email_lower
                for k in (
                    "your registration is successful",
                    "your registration has been successful",
                    "registration successful",
                    "您的预约已成功",
                    "您的預約已成功",
                )
            ):
                return False

        success_keywords = [
            "your registration is successful",
            "your registration has been successful",
            "registration successful",
            "registration has been successful",
            "pleased to inform you that your registration has been successful",
            "您的预约已成功",
            "您的預約已成功",
            "预约成功",
            "預約成功",
        ]

        has_success_keyword = any(keyword in email_lower for keyword in success_keywords)

        if not has_success_keyword:
            return False

        date_normalized = target_date.strip()
        if " " in date_normalized:
            date_normalized = date_normalized.split(" ")[0]
        if "T" in date_normalized:
            date_normalized = date_normalized.split("T")[0]

        if date_normalized in email_content:
            return True

        date_parts = date_normalized.split("-")
        if len(date_parts) == 3:
            year, month, day = date_parts
            if f"{year}-{month}-{day}" in email_content or f"{year}/{month}/{day}" in email_content:
                return True
            # 邮件中月份、日期可能无前导零
            try:
                y, mo, d = int(year), int(month), int(day)
                variants = [
                    f"{y}-{mo}-{d}",
                    f"{y}/{mo}/{d}",
                    f"{y}年{mo}月{d}日",
                ]
                if any(v in email_content for v in variants):
                    return True
            except ValueError:
                pass

        return False
    
    def schedule_task(self, task_id: int, trigger_time: str):
        """调度任务在指定时间执行"""
        try:
            # 解析时间 HH:MM:SS
            parts = trigger_time.split(":")
            hour = int(parts[0])
            minute = int(parts[1])
            second = int(float(parts[2])) if len(parts) > 2 else 0
            
            # 创建今天的触发时间
            now = datetime.now()
            trigger_dt = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
            
            # 如果时间已过，设置为明天
            if trigger_dt <= now:
                trigger_dt += timedelta(days=1)
            
            # 添加调度任务
            job_id = f"booking_task_{task_id}"
            
            # 移除已存在的同名任务
            existing = self.scheduler.get_job(job_id)
            if existing:
                self.scheduler.remove_job(job_id)
            
            self.scheduler.add_job(
                self.execute_booking_task,
                trigger=DateTrigger(run_date=trigger_dt),
                args=[task_id],
                id=job_id,
                replace_existing=True
            )
            
            logger.info(f"任务 {task_id} 已调度: {trigger_dt}")
            return True, f"任务已调度: {trigger_dt.strftime('%Y-%m-%d %H:%M:%S')}"
            
        except Exception as e:
            logger.error(f"调度任务失败: {e}")
            return False, str(e)
    
    def run_task_now(self, task_id: int):
        """立即执行任务"""
        if task_id in self.running_tasks:
            return False, "任务正在运行中"
        
        self.stop_flags[task_id] = False
        thread = threading.Thread(target=self.execute_booking_task, args=[task_id])
        self.running_tasks[task_id] = thread
        thread.start()
        
        return True, "任务已启动"
    
    def stop_task(self, task_id: int):
        """停止任务"""
        if task_id in self.running_tasks:
            self.stop_flags[task_id] = True
            return True, "已发送停止信号"
        return False, "任务未在运行"
    
    def cancel_scheduled_task(self, task_id: int):
        """取消已调度的任务"""
        job_id = f"booking_task_{task_id}"
        try:
            self.scheduler.remove_job(job_id)
            return True, "已取消调度"
        except:
            return False, "任务未在调度中"


# 全局调度器实例
booking_scheduler = BookingScheduler()

