import time
"""
自动补票监控服务
"""
import threading
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import logging
from sqlalchemy.orm import Session

from database import SessionLocal
from models import BookingAccount, BookingTask, SystemConfig, User
from services.email_service import KukuMailService
from apscheduler.triggers.date import DateTrigger
from services.hku_api import HKUApiService

logger = logging.getLogger(__name__)


def generate_random_name() -> str:
    """生成随机中文姓名（最多3个字：姓氏1个 + 名字最多2个）"""
    # 常见姓氏
    surnames = ['李', '王', '张', '刘', '陈', '杨', '赵', '黄', '周', '吴', 
                '徐', '孙', '胡', '朱', '高', '林', '何', '郭', '马', '罗',
                '梁', '宋', '郑', '谢', '韩', '唐', '冯', '于', '董', '萧',
                '程', '曹', '袁', '邓', '许', '傅', '沈', '曾', '彭', '吕']
    # 单字名字用字
    single_chars = ['伟', '芳', '娜', '敏', '静', '丽', '强', '磊', '军',
                    '洋', '勇', '艳', '杰', '娟', '涛', '明', '超', '霞',
                    '平', '刚', '文', '华', '红', '波', '辉', '鹏', '飞',
                    '雪', '梅', '兰', '竹', '菊', '松', '柏', '峰', '山']
    # 双字名字用词（用于双字名）
    double_chars = ['秀英', '秀兰', '桂英', '建华', '建国', '志强', '淑英', 
                    '秀华', '秀珍', '秀芳', '秀梅', '文华', '文静', '文雅',
                    '志明', '志勇', '志华', '建国', '建强', '建明']
    
    surname = random.choice(surnames)
    
    # 随机选择1-2个字的名字（确保总长度不超过3个字）
    if random.random() < 0.7:  # 70%概率单字名
        given_name = random.choice(single_chars)
    else:  # 30%概率双字名
        # 双字名：要么是"单字+单字"，要么是"双字词"
        if random.random() < 0.5:  # 50%概率：单字+单字
            given_name = random.choice(single_chars) + random.choice(single_chars)
        else:  # 50%概率：双字词
            given_name = random.choice(double_chars)
    
    return surname + given_name


def generate_random_id_card() -> str:
    """生成随机身份证后4位（第一位和最后一位不能是0）"""
    # 第一位：1-9
    first = str(random.randint(1, 9))
    # 中间两位：0-9
    middle = ''.join([str(random.randint(0, 9)) for _ in range(2)])
    # 最后一位：1-9
    last = str(random.randint(1, 9))
    
    return first + middle + last


class AutoBookingService:
    """自动补票服务"""
    
    def __init__(self):
        self.running = False
        self.monitor_thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()
    
    def get_config(self, db: Session) -> Dict:
        """获取自动补票配置（返回的key不带auto_booking_前缀，与前端保持一致）"""
        config = {}
        # 定义数据库key到返回key的映射
        key_mapping = {
            "auto_booking_enabled": "enabled",
            "auto_booking_daily_target": "daily_target",
            "auto_booking_account_pool_size": "account_pool_size",
            "auto_booking_monitor_interval": "monitor_interval",
            "auto_booking_auto_create_account": "auto_create_account",
            "auto_booking_auto_create_task": "auto_create_task"
        }
        
        for db_key, return_key in key_mapping.items():
            item = db.query(SystemConfig).filter(SystemConfig.key == db_key).first()
            if item:
                value = item.value
                # 转换类型
                if db_key in ["auto_booking_enabled", "auto_booking_auto_create_account", "auto_booking_auto_create_task"]:
                    config[return_key] = value.lower() == "true" if value else False
                elif db_key in ["auto_booking_daily_target", "auto_booking_account_pool_size", "auto_booking_monitor_interval"]:
                    config[return_key] = int(value) if value else 0
                else:
                    config[return_key] = value
            else:
                # 默认值
                if db_key in ["auto_booking_enabled", "auto_booking_auto_create_account", "auto_booking_auto_create_task"]:
                    config[return_key] = False
                elif db_key in ["auto_booking_daily_target", "auto_booking_account_pool_size", "auto_booking_monitor_interval"]:
                    config[return_key] = 0 if "target" in db_key or "pool" in db_key else 30
                else:
                    config[return_key] = None
        
        return config
    
    def set_config(self, db: Session, config: Dict):
        """设置自动补票配置"""
        for key, value in config.items():
            if not key.startswith("auto_booking_"):
                key = f"auto_booking_{key}"
            
            db_config = db.query(SystemConfig).filter(SystemConfig.key == key).first()
            if db_config:
                db_config.value = str(value)
            else:
                descriptions = {
                    "auto_booking_enabled": "自动补票是否启用",
                    "auto_booking_daily_target": "每天需要抢的名额数",
                    "auto_booking_account_pool_size": "账号池大小",
                    "auto_booking_monitor_interval": "监控间隔（秒）",
                    "auto_booking_auto_create_account": "是否自动创建账号",
                    "auto_booking_auto_create_task": "是否自动创建预约任务"
                }
                db.add(SystemConfig(
                    key=key,
                    value=str(value),
                    description=descriptions.get(key, "")
                ))
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
    
    def get_available_accounts(self, db: Session, limit: int = 10) -> List[BookingAccount]:
        """获取可用的自动创建账号（已登录且从未成功预约过的，且没有正在执行的任务）"""
        from sqlalchemy import not_, exists
        
        # 只获取自动创建的、已登录的、从未成功预约的账号
        accounts = db.query(BookingAccount).filter(
            BookingAccount.is_auto_created == True,
            BookingAccount.hku_token.isnot(None),
            BookingAccount.token_status == "已登录",
            BookingAccount.booking_success_time.is_(None)  # 从未成功预约过
        ).all()
        
        # 进一步过滤：排除有任何 success/pending/running 状态任务的账号
        # 这确保一个账号同时只有一个活跃任务
        usable = []
        for account in accounts:
            # 检查是否有成功任务
            has_success = db.query(BookingTask).filter(
                BookingTask.account_id == account.id,
                BookingTask.status == "success"
            ).first()
            if has_success:
                continue
            
            # 检查是否有正在执行的任务（pending/running/scheduled）
            has_active = db.query(BookingTask).filter(
                BookingTask.account_id == account.id,
                BookingTask.status.in_(["pending", "running", "scheduled"])
            ).first()
            if has_active:
                continue
            
            usable.append(account)
            if len(usable) >= limit:
                break
        
        return usable
    
    def maintain_account_pool(self, db: Session):
        """维护账号池：确保有足够的空闲账号"""
        config = self.get_config(db)
        if not config.get("enabled", False):
            return
        
        pool_size = config.get("account_pool_size", 10)
        if pool_size <= 0:
            return
        
        # 获取当前空闲账号数量
        available_count = len(self.get_available_accounts(db, limit=pool_size * 2))
        
        if available_count < pool_size:
            need_create = pool_size - available_count
            logger.info(f"账号池维护: 当前空闲账号 {available_count}, 目标 {pool_size}, 需要创建 {need_create} 个")
            
            if config.get("auto_create_account", False):
                admin = db.query(User).filter(User.is_admin == True).first()
                owner_id = admin.id if admin else 1
                self.create_account_pool(db, need_create, owner_id)
            else:
                logger.warning("账号池维护: 自动创建账号功能未启用，无法补充账号池")
    
    def get_date_booked_count(self, db: Session, target_date: str) -> int:
        """获取指定日期已预约成功的自动创建账号数量"""
        from sqlalchemy import not_
        # 查询该日期成功的预约任务
        # 注意：现在"已有预约记录"会被标记为failed状态，所以这里只需要统计success状态即可
        # 但为了保险起见，仍然排除可能存在的"已有预约记录"消息
        count = db.query(BookingTask).join(BookingAccount).filter(
            BookingAccount.is_auto_created == True,  # 只统计自动创建的账号
            BookingTask.target_date == target_date,
            BookingTask.status == "success",
            not_(BookingTask.result_message.like("%已有预约记录%"))  # 排除重复预约的情况（以防万一）
        ).count()
        return count
    
    def get_today_booked_count(self, db: Session) -> int:
        """获取今天已预约成功的自动创建账号数量（兼容旧代码）"""
        today = datetime.now().date()
        return self.get_date_booked_count(db, today.strftime("%Y-%m-%d"))
    
    def create_account_pool(self, db: Session, pool_size: int, owner_id: int = 1):
        """创建账号池"""
        config = self.get_global_kuku_config(db)
        if not config["cookie"] or not config["token"] or not config["subtoken"]:
            logger.error("邮箱配置不完整，无法创建账号池")
            return
        
        mail_service = KukuMailService(
            token=config["token"],
            subtoken=config["subtoken"],
            cookie=config["cookie"]
        )
        
        created = 0
        for i in range(pool_size):
            try:
                # 创建临时邮箱
                temp_email = mail_service.create_email()
                if not temp_email:
                    logger.warning(f"创建临时邮箱失败 (第{i+1}个)")
                    continue
                
                # 通过浏览器登录（自动处理 reCAPTCHA + 验证码 + 登录）
                from services.hku_browser_service import BrowserLoginService
                browser = BrowserLoginService(mail_service)
                success, msg, token = browser.auto_login(temp_email)
                if not success or not token:
                    logger.warning(f"登录失败: {msg} (第{i+1}个)")
                    continue
                
                # 生成随机姓名和身份证后4位
                random_name = generate_random_name()
                random_id_card = generate_random_id_card()
                
                # 创建账号记录（标记为自动创建，使用随机生成的姓名和身份证）
                account = BookingAccount(
                    owner_id=owner_id,
                    name=random_name,  # 使用随机生成的姓名
                    email=temp_email,
                    temp_email=temp_email,
                    id_card=random_id_card,  # 使用随机生成的身份证后4位
                    hku_token=token,
                    token_status="已登录",
                    companions=0,
                    entourage_list=[],
                    is_auto_created=True  # 标记为自动创建的账号
                )
                db.add(account)
                db.commit()
                created += 1
                logger.info(f"成功创建账号池账号: {temp_email}")
                
                # 避免请求过快
                time.sleep(2)
            except Exception as e:
                logger.error(f"创建账号池账号异常: {e}")
        
        logger.info(f"账号池创建完成，成功创建 {created}/{pool_size} 个账号")
    
    def monitor_and_book(self, db: Session):
        """监控名额并自动预约（检查今天到两周后的每一天）"""
        config = self.get_config(db)
        
        if not config.get("enabled", False):
            logger.debug("自动补票未启用，跳过")
            return
        
        daily_target = config.get("daily_target", 0)
        if daily_target <= 0:
            logger.debug(f"每天目标为 {daily_target}，跳过")
            return
        
        # 诊断日志：显示当前配置和账号状态
        auto_created_total = db.query(BookingAccount).filter(BookingAccount.is_auto_created == True).count()
        auto_created_logged_in = db.query(BookingAccount).filter(
            BookingAccount.is_auto_created == True,
            BookingAccount.hku_token.isnot(None),
            BookingAccount.token_status == "已登录"
        ).count()
        auto_created_available = len(self.get_available_accounts(db, limit=100))
        all_logged_in = db.query(BookingAccount).filter(
            BookingAccount.hku_token.isnot(None),
            BookingAccount.token_status == "已登录"
        ).count()
        logger.info(f"自动补票诊断: 配置={config}, 自动创建账号总数={auto_created_total}, "
                     f"自动创建已登录={auto_created_logged_in}, 可用账号={auto_created_available}, "
                     f"所有已登录账号={all_logged_in}")
        
        # 计算今天到两周后的所有日期
        today = datetime.now().date()
        dates_to_check = []
        for i in range(14):  # 两周 = 14天
            check_date = today + timedelta(days=i)
            dates_to_check.append(check_date.strftime("%Y-%m-%d"))
        
        logger.info(f"自动补票监控: 检查日期范围 {dates_to_check[0]} 到 {dates_to_check[-1]}, 每天目标 {daily_target}")
        
        # 获取可用名额（需要一个已登录账号的 token）
        # 优先使用自动创建的账号，其次使用任何已登录账号（包括手动创建的）
        monitor_account = db.query(BookingAccount).filter(
            BookingAccount.is_auto_created == True,
            BookingAccount.hku_token.isnot(None),
            BookingAccount.token_status == "已登录"
        ).first()
        
        # 如果没有自动创建的已登录账号，回退到任何已登录账号（手动创建的也行，只用来查询名额）
        if not monitor_account:
            monitor_account = db.query(BookingAccount).filter(
                BookingAccount.hku_token.isnot(None),
                BookingAccount.token_status == "已登录"
            ).first()
            if monitor_account:
                logger.info(f"使用手动创建的账号 {monitor_account.id}({monitor_account.name}) 查询可用名额")
        
        # 如果还是没有，尝试自动创建一个
        if not monitor_account:
            if config.get("auto_create_account", False):
                logger.info("没有任何已登录的账号，自动创建账号用于查询...")
                admin = db.query(User).filter(User.is_admin == True).first()
                owner_id = admin.id if admin else 1
                self.create_account_pool(db, 1, owner_id)
                monitor_account = db.query(BookingAccount).filter(
                    BookingAccount.hku_token.isnot(None),
                    BookingAccount.token_status == "已登录"
                ).first()
            
            if not monitor_account:
                logger.warning("无法获取可用日期: 没有任何已登录的账号可用于查询")
                return
        
        hku_api = HKUApiService(monitor_account.hku_token)
        available_dates = hku_api.get_available_dates()
        
        if not available_dates:
            logger.warning("无法获取可用日期")
            return
        
        logger.info(f"获取到 {len(available_dates)} 个可用日期")
        
        # 打印API返回的所有日期（用于调试）
        api_dates = [day_data.get("date") for day_data in available_dates]
        logger.info(f"API返回的日期: {api_dates[:5]}... (共{len(api_dates)}个)")
        logger.info(f"检查的日期范围: {dates_to_check}")
        
        # 构建日期到可用slot的映射
        date_slots_map = {}
        dates_to_check_set = set(dates_to_check)  # 使用set提高查找效率
        
        for day_data in available_dates:
            date_str = day_data.get("date")
            if not date_str:
                continue
            
            # 如果日期格式包含时间部分，只取日期部分
            original_date_str = date_str
            if " " in date_str:
                date_str = date_str.split(" ")[0]
            # 如果日期格式包含T（ISO格式），只取日期部分
            if "T" in date_str:
                date_str = date_str.split("T")[0]
            
            # 标准化日期格式，确保是 YYYY-MM-DD
            try:
                # 尝试解析日期，确保格式正确
                from datetime import datetime as dt
                parsed_date = dt.strptime(date_str, "%Y-%m-%d")
                date_str = parsed_date.strftime("%Y-%m-%d")
            except:
                logger.warning(f"日期格式解析失败: {original_date_str} -> {date_str}")
                continue
            
            if date_str not in dates_to_check_set:
                logger.debug(f"日期 {date_str} (原始: {original_date_str}) 不在检查范围内，跳过")
                continue  # 只处理今天到两周后的日期
            
            slots = day_data.get("slots", [])
            available_slots = []
            for slot in slots:
                available_quota = slot.get("available_quota", 0)
                if available_quota > 0:
                    available_slots.append({
                        "slot_id": slot.get("id"),
                        "start_time_slot": slot.get("start_time_slot"),
                        "available_quota": available_quota
                    })
            
            if available_slots:
                # 按可用名额排序，优先选择名额多的
                available_slots.sort(key=lambda x: x["available_quota"], reverse=True)
                date_slots_map[date_str] = available_slots
                logger.info(f"{date_str} 找到 {len(available_slots)} 个可用时间段，总可用名额: {sum(s['available_quota'] for s in available_slots)}")
            else:
                logger.debug(f"{date_str} 没有可用名额的时间段")
        
        logger.info(f"构建日期映射完成，共 {len(date_slots_map)} 个日期有可用名额")
        if date_slots_map:
            logger.info(f"有可用名额的日期: {list(date_slots_map.keys())}")
        
        # 只处理有可用名额的日期，为需要补充的日期创建预约任务
        total_tasks_created = 0
        admin = db.query(User).filter(User.is_admin == True).first()
        owner_id = admin.id if admin else 1
        
        # 只检查映射中有可用名额的日期，且这些日期在检查范围内
        dates_with_quota = [date_str for date_str in date_slots_map.keys() if date_str in dates_to_check]
        logger.info(f"开始检查 {len(dates_with_quota)} 个有可用名额的日期（共 {len(dates_to_check)} 个日期）")
        
        if not dates_with_quota:
            logger.warning("没有在检查范围内的有可用名额的日期")
            return
        
        for idx, date_str in enumerate(dates_with_quota):
            try:
                logger.info(f"[{idx+1}/{len(dates_with_quota)}] ========== 开始处理日期: {date_str} ==========")
                
                # 检查该日期已预约数量
                date_booked = self.get_date_booked_count(db, date_str)
                remaining = daily_target - date_booked
                
                if remaining <= 0:
                    logger.info(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 已达成目标: {date_booked}/{daily_target}，跳过")
                    continue
                
                logger.info(f"[{idx+1}/{len(dates_with_quota)}] {date_str}: 目标 {daily_target}, 已预约 {date_booked}, 还需 {remaining}")
                
                # 获取该日期的可用slot（应该已经在映射中）
                if date_str not in date_slots_map:
                    logger.error(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 不在映射中！这不应该发生。映射中的日期: {list(date_slots_map.keys())}")
                    continue
                
                if not date_slots_map[date_str]:
                    logger.warning(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 映射为空")
                    continue
                
                available_slots = date_slots_map[date_str]
                logger.info(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 找到 {len(available_slots)} 个可用时间段，总可用名额: {sum(s['available_quota'] for s in available_slots)}")
                
                # ★ 优化时间段分配：将slots按AM/PM分组，尽量覆盖上午和下午 ★
                am_slots = [s for s in available_slots if s.get("start_time_slot", 20) < 30]
                pm_slots = [s for s in available_slots if s.get("start_time_slot", 20) >= 30]
                # 交替排列：AM, PM, AM, PM... 确保上午下午都覆盖
                interleaved_slots = []
                max_len = max(len(am_slots), len(pm_slots))
                for si in range(max_len):
                    if si < len(am_slots):
                        interleaved_slots.append(am_slots[si])
                    if si < len(pm_slots):
                        interleaved_slots.append(pm_slots[si])
                # 如果只有一种时段，使用原始排序
                if not interleaved_slots:
                    interleaved_slots = available_slots
                logger.info(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 时间段分配: AM={len(am_slots)}个 PM={len(pm_slots)}个, 交替排列后={len(interleaved_slots)}个")
                
                # 获取可用账号（每次重新查询，确保排除已预约成功的账号和正在执行的任务）
                accounts = self.get_available_accounts(db, limit=remaining)
                logger.info(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 当前可用账号数: {len(accounts)}, 需要: {remaining}")
                
                if len(accounts) == 0:
                    logger.warning(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 没有可用账号！")
                
                # 如果账号不足，尝试自动创建新账号
                if len(accounts) < remaining and config.get("auto_create_account", False):
                    need_create = remaining - len(accounts)
                    logger.info(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 账号不足，需要创建 {need_create} 个账号")
                    self.create_account_pool(db, need_create, owner_id)
                    # 重新获取账号
                    accounts = self.get_available_accounts(db, limit=remaining)
                    logger.info(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 创建账号后，可用账号数: {len(accounts)}")
                elif len(accounts) < remaining:
                    logger.warning(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 账号不足（{len(accounts)}/{remaining}），自动创建未启用，将使用现有 {len(accounts)} 个账号")
                
                # 为该日期的每个账号创建预约任务
                if len(accounts) == 0:
                    logger.warning(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 没有任何可用账号，无法创建预约任务，跳过该日期")
                    # 尝试用auto_create_account创建
                    if config.get("auto_create_account", False):
                        logger.info(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 尝试创建 {remaining} 个账号...")
                        self.create_account_pool(db, remaining, owner_id)
                        accounts = self.get_available_accounts(db, limit=remaining)
                        if len(accounts) == 0:
                            logger.error(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 创建账号后仍然没有可用账号，跳过")
                            continue
                        logger.info(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 成功创建 {len(accounts)} 个账号")
                    else:
                        logger.warning(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 没有可用账号且自动创建未启用，跳过")
                        continue
                
                logger.info(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 开始为 {min(len(accounts), remaining)} 个账号创建预约任务（AM/PM智能分配）")
                tasks_created_for_date = 0
                used_account_ids = set()  # 记录本日期已使用的账号ID，避免重复使用
                am_count = 0  # 已创建的上午任务数
                pm_count = 0  # 已创建的下午任务数
                
                for account in accounts:
                    if tasks_created_for_date >= remaining:
                        break
                    
                    # ★ 关键检查：在创建任务前，全面检查账号状态 ★
                    db.refresh(account)  # 刷新账号状态，获取最新的 booking_success_time
                    
                    # 1. 检查账号是否已经预约成功（通过 booking_success_time）
                    if account.booking_success_time is not None:
                        logger.warning(f"[{idx+1}/{len(dates_with_quota)}] 账号 {account.id} 已经预约成功（booking_success_time={account.booking_success_time}），跳过")
                        continue
                    
                    # 2. 检查该账号是否在本日期已使用（防止同一循环内重复）
                    if account.id in used_account_ids:
                        logger.warning(f"[{idx+1}/{len(dates_with_quota)}] 账号 {account.id} 在本日期已使用，跳过")
                        continue
                    
                    # 3. ★ 关键检查：该账号是否在任何日期有任何活跃任务（pending/running/scheduled/success）★
                    any_active_task = db.query(BookingTask).filter(
                        BookingTask.account_id == account.id,
                        BookingTask.status.in_(["pending", "running", "scheduled", "success"])
                    ).first()
                    
                    if any_active_task:
                        logger.warning(f"[{idx+1}/{len(dates_with_quota)}] 账号 {account.id} 已有活跃任务(#{any_active_task.id} {any_active_task.target_date} {any_active_task.status})，跳过（防止重复预约）")
                        # 如果任务已成功，更新账号状态
                        if any_active_task.status == "success" and account.booking_success_time is None:
                            account.booking_success_time = datetime.utcnow()
                            db.commit()
                        continue
                    
                    # 4. 检查是否已有该账号该日期的任务（额外保险）
                    existing_task = db.query(BookingTask).filter(
                        BookingTask.account_id == account.id,
                        BookingTask.target_date == date_str,
                        BookingTask.status.in_(["pending", "running", "scheduled", "success"])
                    ).first()
                    
                    if existing_task:
                        logger.warning(f"[{idx+1}/{len(dates_with_quota)}] 账号 {account.id} 在 {date_str} 已有任务（状态: {existing_task.status}），跳过")
                        continue
                    
                    # ★ 智能时间段分配：优先分配给已分配较少的时段，确保上午下午都覆盖 ★
                    slot = None
                    if len(am_slots) > 0 and len(pm_slots) > 0:
                        # 如果上午和下午都有，优先分配给数量较少的时段
                        if am_count < pm_count:
                            # 上午任务少，优先分配上午
                            slot = am_slots[am_count % len(am_slots)]
                        elif pm_count < am_count:
                            # 下午任务少，优先分配下午
                            slot = pm_slots[pm_count % len(pm_slots)]
                        else:
                            # 数量相等，交替分配（优先上午）
                            if tasks_created_for_date % 2 == 0:
                                slot = am_slots[am_count % len(am_slots)]
                            else:
                                slot = pm_slots[pm_count % len(pm_slots)]
                    elif len(am_slots) > 0:
                        # 只有上午
                        slot = am_slots[am_count % len(am_slots)]
                    elif len(pm_slots) > 0:
                        # 只有下午
                        slot = pm_slots[pm_count % len(pm_slots)]
                    else:
                        # 备用：使用交替排列的slots
                        slot = interleaved_slots[tasks_created_for_date % len(interleaved_slots)]
                    
                    if not slot:
                        logger.error(f"[{idx+1}/{len(dates_with_quota)}] {date_str} 无法选择时间段，跳过")
                        continue
                    
                    # 监控发现有名额时，应该立即去预约（名额随时可能被抢走）
                    now = datetime.now()
                    trigger_time = now.strftime("%H:%M:%S")  # 使用当前时间作为触发时间记录
                    
                    # 根据 slot 的 start_time_slot 判断 AM/PM
                    start_time_slot = slot.get("start_time_slot", 20)
                    time_slot = "AM" if start_time_slot < 30 else "PM"
                    
                    # 创建预约任务
                    task = BookingTask(
                        account_id=account.id,
                        task_mode="rapid",  # 使用极速模式
                        target_date=date_str,
                        time_slot=time_slot,
                        slot_id=slot["slot_id"],
                        trigger_time=trigger_time,
                        max_retries=15,
                        auto_login=False,  # 账号已登录，不需要自动登录
                        status="pending"
                    )
                    db.add(task)
                    db.commit()
                    db.refresh(task)
                    
                    logger.info(f"[{idx+1}/{len(dates_with_quota)}] ✅ 为账号 {account.id} 创建自动补票任务: {date_str} slot_id={slot['slot_id']} time_slot={time_slot}")
                    
                    # 更新时段计数
                    if time_slot == "AM":
                        am_count += 1
                    else:
                        pm_count += 1
                    
                    # 标记账号已使用
                    used_account_ids.add(account.id)
                    tasks_created_for_date += 1
                    total_tasks_created += 1
                    
                    # 有名额时立即执行预约任务（在后台线程中）
                    if config.get("auto_create_task", True):
                        logger.info(f"[{idx+1}/{len(dates_with_quota)}] 🚀 立即执行预约任务 #{task.id}: {date_str} slot_id={slot['slot_id']}")
                        threading.Thread(
                            target=self._execute_booking_task,
                            args=(task.id,),
                            daemon=True
                        ).start()
                
                logger.info(f"[{idx+1}/{len(dates_with_quota)}] ========== 日期 {date_str} 处理完成，本日期创建了 {tasks_created_for_date} 个预约任务（AM: {am_count}个, PM: {pm_count}个） ==========")
                
                # 在处理完一个日期后，提交事务并刷新，确保后续日期能获取到最新的账号状态
                db.commit()
                
                # 短暂延迟，确保数据库状态已更新（给后台任务一些时间执行）
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"[{idx+1}/{len(dates_with_quota)}] 处理日期 {date_str} 时发生异常: {e}", exc_info=True)
                continue
        
        logger.info(f"自动补票: 共创建了 {total_tasks_created} 个预约任务")
    
    def _execute_booking_task(self, task_id: int):
        """执行预约任务（在后台线程中）"""
        try:
            from services.scheduler_service import booking_scheduler
            # 使用调度器的执行方法
            booking_scheduler.execute_booking_task(task_id)
        except Exception as e:
            logger.error(f"执行自动补票任务异常: {e}")
    
    def start_monitoring(self):
        """启动监控"""
        with self.lock:
            if self.running:
                return
            self.running = True
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
            logger.info("自动补票监控已启动")
    
    def stop_monitoring(self):
        """停止监控"""
        with self.lock:
            if not self.running:
                return
            self.running = False
            logger.info("自动补票监控已停止")
    
    def _prepare_for_new_day_booking(self, db: Session):
        """为新的一天准备账号（在8点59分时调用，只确保账号就绪）"""
        config = self.get_config(db)
        if not config.get("enabled", False):
            return
        
        daily_target = config.get("daily_target", 0)
        if daily_target <= 0:
            return
        
        logger.info(f"========== [8:59准备阶段] 开始准备账号，每天目标: {daily_target} ==========")
        
        # 确保有足够的可用账号
        available_accounts = self.get_available_accounts(db, limit=daily_target * 2)
        prepared_count = len(available_accounts)
        
        if prepared_count < daily_target:
            need_create = daily_target - prepared_count
            logger.info(f"[8:59准备阶段] 当前可用账号 {prepared_count}，需要 {daily_target}，需创建 {need_create} 个")
            
            if need_create > 0 and config.get("auto_create_account", False):
                admin = db.query(User).filter(User.is_admin == True).first()
                owner_id = admin.id if admin else 1
                self.create_account_pool(db, need_create, owner_id)
                # 重新获取账号列表
                available_accounts = self.get_available_accounts(db, limit=daily_target * 2)
                prepared_count = len(available_accounts)
                logger.info(f"[8:59准备阶段] 创建账号后，可用账号数: {prepared_count}")
            else:
                logger.warning(f"[8:59准备阶段] 需要创建账号但自动创建功能未启用")
        
        logger.info(f"========== [8:59准备阶段] 完成，可用账号: {prepared_count}/{daily_target} ==========")
    
    def _snipe_new_day(self, db: Session):
        """9点整抢新一天的名额（在9:00:00时调用）"""
        from services.scheduler_service import booking_scheduler
        
        config = self.get_config(db)
        if not config.get("enabled", False):
            return
        
        daily_target = config.get("daily_target", 0)
        if daily_target <= 0:
            return
        
        logger.info(f"========== [9点抢票] 开始抢新一天的名额! ==========")
        
        # 获取一个已登录账号的token查询最新名额
        monitor_account = db.query(BookingAccount).filter(
            BookingAccount.is_auto_created == True,
            BookingAccount.hku_token.isnot(None),
            BookingAccount.token_status == "已登录"
        ).first()
        
        if not monitor_account:
            # 尝试使用任何已登录账号
            monitor_account = db.query(BookingAccount).filter(
                BookingAccount.hku_token.isnot(None),
                BookingAccount.token_status == "已登录"
            ).first()
        
        if not monitor_account:
            logger.error(f"[9点抢票] 没有已登录的账号可用于查询，放弃抢票")
            return
        
        hku_api = HKUApiService(monitor_account.hku_token)
        
        # 重试获取可用日期（9点刚开放可能需要几次尝试）
        available_dates = None
        for attempt in range(5):
            available_dates = hku_api.get_available_dates()
            if available_dates:
                break
            logger.warning(f"[9点抢票] 第 {attempt+1} 次获取可用日期失败，1秒后重试...")
            time.sleep(1)
        
        if not available_dates:
            logger.error(f"[9点抢票] 无法获取可用日期，放弃抢票")
            return
        
        logger.info(f"[9点抢票] 获取到 {len(available_dates)} 个日期数据")
        
        # 找到所有有可用名额的日期，特别关注新开放的日期
        today = datetime.now().date()
        new_slots_found = {}
        
        for day_data in available_dates:
            date_str = day_data.get("date", "")
            if " " in date_str:
                date_str = date_str.split(" ")[0]
            if "T" in date_str:
                date_str = date_str.split("T")[0]
            
            try:
                date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
            except:
                continue
            
            # 只处理今天到两周后的日期
            if date_obj < today or date_obj > today + timedelta(days=14):
                continue
            
            # 检查该日期是否已达成目标
            date_booked = self.get_date_booked_count(db, date_str)
            remaining = daily_target - date_booked
            if remaining <= 0:
                continue
            
            slots = day_data.get("slots", [])
            available_slots = []
            for slot in slots:
                available_quota = slot.get("available_quota", 0)
                if available_quota > 0:
                    available_slots.append({
                        "slot_id": slot.get("id"),
                        "start_time_slot": slot.get("start_time_slot"),
                        "available_quota": available_quota
                    })
            
            if available_slots:
                # ★ 优化时间段分配：将slots按AM/PM分组，尽量覆盖上午和下午 ★
                am_slots = [s for s in available_slots if s.get("start_time_slot", 20) < 30]
                pm_slots = [s for s in available_slots if s.get("start_time_slot", 20) >= 30]
                # 交替排列：AM, PM, AM, PM...
                interleaved_slots = []
                max_len = max(len(am_slots), len(pm_slots))
                for si in range(max_len):
                    if si < len(am_slots):
                        interleaved_slots.append(am_slots[si])
                    if si < len(pm_slots):
                        interleaved_slots.append(pm_slots[si])
                if not interleaved_slots:
                    interleaved_slots = available_slots
                
                new_slots_found[date_str] = {
                    "slots": interleaved_slots,  # 使用交替排列的slots
                    "remaining": remaining
                }
                logger.info(f"[9点抢票] {date_str} 时间段分配: AM={len(am_slots)}个 PM={len(pm_slots)}个, 交替排列后={len(interleaved_slots)}个")
        
        if not new_slots_found:
            logger.info(f"[9点抢票] 没有发现需要抢的名额（所有日期已达标或没有可用名额）")
            return
        
        logger.info(f"[9点抢票] 发现 {len(new_slots_found)} 个日期需要抢票: {list(new_slots_found.keys())}")
        
        # 获取可用账号
        total_needed = sum(info["remaining"] for info in new_slots_found.values())
        available_accounts = self.get_available_accounts(db, limit=total_needed * 2)
        
        if len(available_accounts) == 0:
            logger.error(f"[9点抢票] 没有可用账号，无法抢票")
            return
        
        logger.info(f"[9点抢票] 可用账号: {len(available_accounts)}，总共需要: {total_needed}")
        
        # 为每个日期创建任务并立即执行
        admin = db.query(User).filter(User.is_admin == True).first()
        owner_id = admin.id if admin else 1
        total_tasks = 0
        global_used_account_ids = set()
        
        for date_str, info in new_slots_found.items():
            slots = info["slots"]  # 这是交替排列的slots
            remaining = info["remaining"]
            tasks_for_date = 0
            
            # 重新分组AM/PM（因为slots已经是交替排列的，需要重新分组用于智能分配）
            am_slots = [s for s in slots if s.get("start_time_slot", 20) < 30]
            pm_slots = [s for s in slots if s.get("start_time_slot", 20) >= 30]
            am_count = 0  # 已创建的上午任务数
            pm_count = 0  # 已创建的下午任务数
            
            # 为该日期分配账号
            for account in available_accounts:
                if tasks_for_date >= remaining:
                    break
                
                if account.id in global_used_account_ids:
                    continue
                
                # ★ 关键检查：在创建任务前，全面检查账号状态 ★
                db.refresh(account)
                
                # 1. 检查账号是否已经预约成功
                if account.booking_success_time is not None:
                    logger.warning(f"[9点抢票] 账号 {account.id} 已经预约成功，跳过")
                    continue
                
                # 2. ★ 关键检查：该账号是否在任何日期有任何活跃任务（pending/running/scheduled/success）★
                any_active_task = db.query(BookingTask).filter(
                    BookingTask.account_id == account.id,
                    BookingTask.status.in_(["pending", "running", "scheduled", "success"])
                ).first()
                
                if any_active_task:
                    logger.warning(f"[9点抢票] 账号 {account.id} 已有活跃任务(#{any_active_task.id} {any_active_task.target_date} {any_active_task.status})，跳过（防止重复预约）")
                    # 如果任务已成功，更新账号状态
                    if any_active_task.status == "success" and account.booking_success_time is None:
                        account.booking_success_time = datetime.utcnow()
                        db.commit()
                    continue
                
                # 3. 检查是否已有该账号该日期的任务（额外保险）
                existing_task = db.query(BookingTask).filter(
                    BookingTask.account_id == account.id,
                    BookingTask.target_date == date_str,
                    BookingTask.status.in_(["pending", "scheduled", "running", "success"])
                ).first()
                if existing_task:
                    logger.warning(f"[9点抢票] 账号 {account.id} 在 {date_str} 已有任务，跳过")
                    continue
                
                # ★ 智能时间段分配：优先分配给已分配较少的时段，确保上午下午都覆盖 ★
                slot = None
                if len(am_slots) > 0 and len(pm_slots) > 0:
                    # 如果上午和下午都有，优先分配给数量较少的时段
                    if am_count < pm_count:
                        # 上午任务少，优先分配上午
                        slot = am_slots[am_count % len(am_slots)]
                    elif pm_count < am_count:
                        # 下午任务少，优先分配下午
                        slot = pm_slots[pm_count % len(pm_slots)]
                    else:
                        # 数量相等，交替分配（优先上午）
                        if tasks_for_date % 2 == 0:
                            slot = am_slots[am_count % len(am_slots)]
                        else:
                            slot = pm_slots[pm_count % len(pm_slots)]
                elif len(am_slots) > 0:
                    # 只有上午
                    slot = am_slots[am_count % len(am_slots)]
                elif len(pm_slots) > 0:
                    # 只有下午
                    slot = pm_slots[pm_count % len(pm_slots)]
                else:
                    # 备用：使用交替排列的slots
                    slot = slots[tasks_for_date % len(slots)]
                
                if not slot:
                    logger.error(f"[9点抢票] {date_str} 无法选择时间段，跳过")
                    continue
                
                start_time_slot = slot.get("start_time_slot", 20)
                time_slot = "AM" if start_time_slot < 30 else "PM"
                
                task = BookingTask(
                    account_id=account.id,
                    task_mode="rapid",
                    target_date=date_str,
                    time_slot=time_slot,
                    slot_id=slot["slot_id"],
                    trigger_time="09:00:00",
                    max_retries=15,
                    auto_login=False,
                    status="pending"
                )
                db.add(task)
                db.commit()
                db.refresh(task)
                
                # 更新时段计数
                if time_slot == "AM":
                    am_count += 1
                else:
                    pm_count += 1
                
                # 立即执行！
                logger.info(f"[9点抢票] 🚀 创建并立即执行任务 #{task.id}: 账号{account.id} -> {date_str} slot_id={slot['slot_id']} time_slot={time_slot}")
                threading.Thread(
                    target=self._execute_booking_task,
                    args=(task.id,),
                    daemon=True
                ).start()
                
                global_used_account_ids.add(account.id)
                tasks_for_date += 1
                total_tasks += 1
            
            logger.info(f"[9点抢票] {date_str} 创建了 {tasks_for_date} 个任务（AM: {am_count}个, PM: {pm_count}个）")
        
        logger.info(f"========== [9点抢票] 完成! 共创建并执行 {total_tasks} 个任务 ==========")
    
    
    def _monitor_loop(self):
        """监控循环"""
        last_prepare_date = None  # 记录上次准备日期（确保每天只执行一次）
        last_snipe_date = None  # 记录上次抢票日期（确保每天只执行一次）
        
        while self.running:
            try:
                db = SessionLocal()
                try:
                    config = self.get_config(db)
                    interval = config.get("monitor_interval", 30)
                    
                    if config.get("enabled", False):
                        now = datetime.now()
                        current_hour = now.hour
                        current_minute = now.minute
                        current_date = now.date()
                        
                        # === 8:59 准备阶段：只确保账号就绪 ===
                        if current_hour == 8 and current_minute >= 55:
                            if last_prepare_date != current_date:
                                logger.info("========== 检测到8:55+，开始准备账号 ==========")
                                self._prepare_for_new_day_booking(db)
                                last_prepare_date = current_date
                        
                        # === 9:00 抢票阶段：获取最新名额并立即抢 ===
                        if current_hour == 9 and current_minute <= 5:
                            if last_snipe_date != current_date:
                                logger.info("========== 检测到9:00，开始抢新一天名额！ ==========")
                                self._snipe_new_day(db)
                                last_snipe_date = current_date
                        
                        # 维护账号池
                        self.maintain_account_pool(db)
                        # 监控并预约（常规监控，处理所有有名额的日期）
                        self.monitor_and_book(db)
                    
                    # 根据时间段调整等待间隔
                    # 8:55-9:10 期间使用更短的间隔（3秒），确保不错过窗口
                    now = datetime.now()
                    if now.hour == 8 and now.minute >= 55:
                        wait_interval = 3
                    elif now.hour == 9 and now.minute <= 10:
                        wait_interval = 3
                    else:
                        wait_interval = interval
                    
                    # 等待指定间隔
                    for _ in range(wait_interval):
                        if not self.running:
                            break
                        time.sleep(1)
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"自动补票监控循环异常: {e}", exc_info=True)
                time.sleep(10)


# 全局自动补票服务实例
auto_booking_service = AutoBookingService()

