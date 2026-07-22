"""
港大预约系统 - 后端API
"""
from fastapi import FastAPI, Depends, HTTPException, status, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from datetime import timedelta, datetime, timezone
from typing import List, Optional
import logging
import sys
import threading
import time
import webbrowser

from database import engine, get_db, SessionLocal, Base
from models import User, BookingAccount, BookingTask, TaskLog, SystemConfig
from schemas import (
    UserCreate, UserResponse, UserUpdate, Token,
    BookingAccountCreate, BookingAccountUpdate, BookingAccountResponse,
    BookingTaskCreate, BookingTaskUpdate, BookingTaskResponse,
    KukuMailConfig, LoginRequest, SendCodeRequest, TaskLogResponse,
    ChangePasswordRequest, AutoBookingConfig, TransferAppointmentRequest,
    ClearLogsRequest, TaskBatchDeleteByFilterRequest,
)
from auth import (
    get_password_hash, verify_password, authenticate_user, create_access_token,
    get_current_active_user, get_current_admin_user, ACCESS_TOKEN_EXPIRE_MINUTES
)
from config import DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD
from services.hku_api import HKUApiService
from services.email_service import KukuMailService
from services.scheduler_service import booking_scheduler
from services.auto_booking_service import auto_booking_service

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 创建数据库表
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="港大预约系统 API",
    description="香港大学参观预约自动化系统",
    version="1.0.0"
)

# CORS配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== 全局邮箱配置辅助函数 ==========
def get_global_kuku_config(db: Session) -> dict:
    """获取全局邮箱配置"""
    cookie = db.query(SystemConfig).filter(SystemConfig.key == "kuku_cookie").first()
    token = db.query(SystemConfig).filter(SystemConfig.key == "kuku_token").first()
    subtoken = db.query(SystemConfig).filter(SystemConfig.key == "kuku_subtoken").first()
    return {
        "cookie": cookie.value if cookie else None,
        "token": token.value if token else None,
        "subtoken": subtoken.value if subtoken else None
    }


def get_global_kuku_cookie(db: Session) -> Optional[str]:
    """获取全局邮箱Cookie（兼容旧接口）"""
    cookie = db.query(SystemConfig).filter(SystemConfig.key == "kuku_cookie").first()
    return cookie.value if cookie else None


def set_global_kuku_cookie(db: Session, cookie: str):
    """设置全局邮箱Cookie"""
    config = db.query(SystemConfig).filter(SystemConfig.key == "kuku_cookie").first()
    if config:
        config.value = cookie
    else:
        config = SystemConfig(key="kuku_cookie", value=cookie, description="Kuku邮箱Cookie")
        db.add(config)
    db.commit()


def auto_create_email_and_login(db: Session, account: BookingAccount) -> tuple[bool, str]:
    """自动创建邮箱并登录获取Token"""
    config = get_global_kuku_config(db)
    if not config["cookie"] or not config["token"] or not config["subtoken"]:
        return False, "邮箱配置不完整，请在系统设置中配置 Token、SubToken 和 Cookie"
    
    try:
        # 1. 创建临时邮箱
        mail_service = KukuMailService(
            token=config["token"],
            subtoken=config["subtoken"],
            cookie=config["cookie"]
        )
        
        logger.info(f"[{account.name}] 正在创建临时邮箱...")
        temp_email = mail_service.create_email()
        
        if not temp_email:
            return False, "创建临时邮箱失败"
        
        account.temp_email = temp_email
        account.email = temp_email
        db.commit()
        
        logger.info(f"[{account.name}] 临时邮箱: {temp_email}")
        
        # 2. 发送验证码
        hku_api = HKUApiService()
        success, msg = hku_api.send_verification_code(temp_email)
        
        if not success:
            return False, f"发送验证码失败: {msg}"
        
        logger.info(f"[{account.name}] 验证码已发送，等待接收...")
        
        # 3. 等待并提取验证码
        code = mail_service.get_verification_code(temp_email, timeout=90)
        
        if not code:
            return False, "获取验证码失败或超时"
        
        logger.info(f"[{account.name}] 验证码: {code}")
        
        # 4. 登录获取Token
        token, name, id_card = hku_api.login(temp_email, code)
        
        if not token:
            return False, "登录失败"
        
        # 更新账号Token
        account.hku_token = token
        account.token_status = "已登录"
        account.updated_at = datetime.utcnow()
        db.commit()
        
        logger.info(f"[{account.name}] 登录成功，Token已更新")
        return True, f"登录成功，邮箱: {temp_email}"
        
    except Exception as e:
        logger.error(f"[{account.name}] 自动登录异常: {str(e)}")
        return False, f"自动登录异常: {str(e)}"


def daily_refresh_all_tokens():
    """每日刷新所有账号Token"""
    logger.info("开始每日Token刷新任务...")
    db = SessionLocal()
    
    try:
        accounts = db.query(BookingAccount).all()
        success_count = 0
        fail_count = 0
        
        for account in accounts:
            logger.info(f"刷新账号: {account.name}")
            ok, msg = auto_create_email_and_login(db, account)
            if ok:
                success_count += 1
            else:
                fail_count += 1
                logger.error(f"[{account.name}] 刷新失败: {msg}")
            
            # 每个账号之间间隔一下，避免请求过快
            time.sleep(5)
        
        logger.info(f"每日Token刷新完成: 成功 {success_count}, 失败 {fail_count}")
    except Exception as e:
        logger.error(f"每日Token刷新异常: {str(e)}")
    finally:
        db.close()


# ========== 启动/关闭事件 ==========
@app.on_event("startup")
async def startup_event():
    """应用启动时执行"""
    # 数据库迁移：为已有表添加新字段
    try:
        from sqlalchemy import text, inspect
        inspector = inspect(engine)
        
        with engine.begin() as conn:  # 使用 begin() 自动管理事务
            # 检查 booking_tasks 表
            if "booking_tasks" in inspector.get_table_names():
                columns = [col["name"] for col in inspector.get_columns("booking_tasks")]
                if "task_mode" not in columns:
                    conn.execute(text("ALTER TABLE booking_tasks ADD COLUMN task_mode VARCHAR(10) DEFAULT 'stable'"))
                    logger.info("数据库迁移: 已添加 task_mode 字段")
                if "confirmation_email" not in columns:
                    conn.execute(text("ALTER TABLE booking_tasks ADD COLUMN confirmation_email TEXT"))
                    logger.info("数据库迁移: 已添加 confirmation_email 字段")

                # 检查 booking_accounts 表
                if "booking_accounts" in inspector.get_table_names():
                    account_columns = [col["name"] for col in inspector.get_columns("booking_accounts")]
                    if "booking_success_time" not in account_columns:
                        conn.execute(text("ALTER TABLE booking_accounts ADD COLUMN booking_success_time DATETIME"))
                        logger.info("数据库迁移: 已添加 booking_success_time 字段")
                    if "is_auto_created" not in account_columns:
                        conn.execute(text("ALTER TABLE booking_accounts ADD COLUMN is_auto_created BOOLEAN DEFAULT 0"))
                        logger.info("数据库迁移: 已添加 is_auto_created 字段")
    except Exception as e:
        logger.warning(f"数据库迁移检查: {e}")
    
    # 初始化默认管理员
    db = next(get_db())
    admin = db.query(User).filter(User.username == DEFAULT_ADMIN_USERNAME).first()
    if not admin:
        admin = User(
            username=DEFAULT_ADMIN_USERNAME,
            hashed_password=get_password_hash(DEFAULT_ADMIN_PASSWORD),
            is_admin=True
        )
        db.add(admin)
        db.commit()
        logger.info(f"已创建默认管理员账号: {DEFAULT_ADMIN_USERNAME}")
    
    # 初始化每日刷新时间配置
    refresh_time = db.query(SystemConfig).filter(SystemConfig.key == "daily_refresh_time").first()
    if not refresh_time:
        refresh_time = SystemConfig(key="daily_refresh_time", value="06:00:00", description="每日Token刷新时间")
        db.add(refresh_time)
        db.commit()
    
    db.close()
    
    # 启动调度器
    booking_scheduler.start()
    
    # 添加每日刷新任务
    from apscheduler.triggers.cron import CronTrigger
    booking_scheduler.scheduler.add_job(
        daily_refresh_all_tokens,
        CronTrigger(hour=6, minute=0),  # 默认每天6点执行
        id="daily_token_refresh",
        replace_existing=True
    )
    
    # 恢复所有未执行的预约任务调度（解决服务重启后任务丢失的问题）
    restore_db = SessionLocal()
    try:
        pending_tasks = restore_db.query(BookingTask).filter(
            BookingTask.status.in_(["pending", "scheduled"])
        ).all()
        restored_count = 0
        for task in pending_tasks:
            success, msg = booking_scheduler.schedule_task(task.id, task.trigger_time)
            if success:
                task.status = "scheduled"
                restored_count += 1
                logger.info(f"恢复任务调度: 任务#{task.id} {task.target_date} {task.trigger_time} -> {msg}")
            else:
                logger.warning(f"恢复任务调度失败: 任务#{task.id} -> {msg}")
        restore_db.commit()
        logger.info(f"共恢复 {restored_count}/{len(pending_tasks)} 个待执行任务的调度")
    except Exception as e:
        logger.error(f"恢复任务调度异常: {e}")
    finally:
        restore_db.close()
    
    logger.info("预约调度器已启动，每日Token刷新任务已添加")
    
    # 启动自动补票监控服务
    auto_booking_service.start_monitoring()
    logger.info("自动补票监控服务已启动")


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时执行"""
    auto_booking_service.stop_monitoring()
    booking_scheduler.stop()
    logger.info("自动补票监控服务已停止，预约调度器已停止")


# ========== 认证接口 ==========
@app.post("/api/auth/login", response_model=Token, tags=["认证"])
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    """用户登录"""
    user = authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.get("/api/auth/me", response_model=UserResponse, tags=["认证"])
async def get_current_user_info(
    current_user: User = Depends(get_current_active_user)
):
    """获取当前用户信息"""
    return current_user


@app.post("/api/auth/change-password", tags=["认证"])
async def change_password(
    password_data: ChangePasswordRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    """更改当前用户密码"""
    # 验证旧密码
    if not verify_password(password_data.old_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="旧密码不正确"
        )
    
    # 验证新密码长度
    if len(password_data.new_password) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="新密码长度至少为6位"
        )
    
    # 更新密码
    current_user.hashed_password = get_password_hash(password_data.new_password)
    db.commit()
    db.refresh(current_user)
    
    return {"message": "密码修改成功"}


# ========== 系统配置（管理员） ==========
@app.get("/api/system/kuku-config", tags=["系统配置"])
async def get_kuku_config(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    """获取全局邮箱配置"""
    config = get_global_kuku_config(db)
    if config["cookie"] and config["token"] and config["subtoken"]:
        return {
            "configured": True,
            "token": config["token"][:8] + "..." if config["token"] else "",
            "subtoken": config["subtoken"][:8] + "..." if config["subtoken"] else "",
            "cookie_length": len(config["cookie"]) if config["cookie"] else 0
        }
    return {"configured": False}


@app.get("/api/system/kuku-cookie", tags=["系统配置"])
async def get_kuku_cookie_for_browser(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    """获取邮箱Cookie（用于浏览器打开邮箱页面）"""
    config = get_global_kuku_config(db)
    if not config["cookie"]:
        raise HTTPException(status_code=404, detail="未配置邮箱Cookie")
    
    return {
        "cookie": config["cookie"],
        "cookie_length": len(config["cookie"])
    }


@app.get("/open-kuku-mail", response_class=HTMLResponse, tags=["系统配置"])
async def open_kuku_mail_page(
    request: Request,
    cookie: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """打开邮箱页面的中间页面（设置Cookie并跳转）"""
    # 从URL参数获取cookie，如果没有则从数据库读取
    if not cookie:
        config = get_global_kuku_config(db)
        cookie = config.get("cookie", "")
    
    if not cookie:
        return HTMLResponse("""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <title>邮箱配置未设置</title>
        </head>
        <body>
            <h1>错误</h1>
            <p>邮箱Cookie未配置，请在系统设置中配置。</p>
        </body>
        </html>
        """)
    
    # 创建HTML页面，尝试设置cookie并跳转
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>正在打开邮箱...</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                background: #1e1e1e;
                color: #fff;
            }}
            .container {{
                text-align: center;
                padding: 20px;
            }}
            .cookie-info {{
                background: #2d2d2d;
                padding: 15px;
                border-radius: 8px;
                margin: 20px 0;
                word-break: break-all;
                font-size: 12px;
                max-width: 600px;
            }}
            button {{
                background: #3b82f6;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 5px;
                cursor: pointer;
                margin: 5px;
            }}
            button:hover {{
                background: #2563eb;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h2>正在打开邮箱页面...</h2>
            <p>由于浏览器安全限制，无法直接为其他域名设置Cookie。</p>
            <div class="cookie-info">
                <strong>Cookie信息（已复制到剪贴板）：</strong><br>
                <textarea id="cookieText" readonly style="width: 100%; height: 100px; margin-top: 10px; padding: 10px; background: #1a1a1a; color: #fff; border: 1px solid #444; border-radius: 4px;">{cookie}</textarea>
            </div>
            <div>
                <button onclick="copyCookie()">复制Cookie</button>
                <button onclick="openMail()">打开邮箱页面</button>
            </div>
            <p style="font-size: 12px; color: #888; margin-top: 20px;">
                提示：打开邮箱页面后，按F12打开开发者工具，在Console中执行：<br>
                <code style="background: #1a1a1a; padding: 5px; border-radius: 3px;">document.cookie = `{cookie}`</code>
            </p>
        </div>
        <script>
            // 自动复制cookie到剪贴板
            function copyCookie() {{
                const textarea = document.getElementById('cookieText');
                textarea.select();
                document.execCommand('copy');
                alert('Cookie已复制到剪贴板！');
            }}
            
            function openMail() {{
                window.open('https://m.kuku.lu/recv.php', '_blank');
            }}
            
            // 页面加载时自动复制
            window.onload = function() {{
                copyCookie();
                // 延迟打开邮箱页面
                setTimeout(function() {{
                    openMail();
                }}, 1000);
            }};
        </script>
    </body>
    </html>
    """
    
    return HTMLResponse(html_content)


@app.post("/api/system/kuku-config", tags=["系统配置"])
async def set_kuku_config_api(
    config: KukuMailConfig,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    """设置全局邮箱配置（三个参数都必填）"""
    if not config.token or len(config.token) != 32:
        raise HTTPException(status_code=400, detail="Token 必须是32位字符")
    if not config.subtoken or len(config.subtoken) != 32:
        raise HTTPException(status_code=400, detail="SubToken 必须是32位字符")
    if not config.cookie or len(config.cookie) < 10:
        raise HTTPException(status_code=400, detail="Cookie不能为空")
    
    # 保存配置
    for key, value, desc in [
        ("kuku_token", config.token, "Kuku Token"),
        ("kuku_subtoken", config.subtoken, "Kuku SubToken"),
        ("kuku_cookie", config.cookie, "Kuku Cookie"),
    ]:
        db_config = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        if db_config:
            db_config.value = value
        else:
            db.add(SystemConfig(key=key, value=value, description=desc))
    db.commit()
    
    return {
        "success": True, 
        "message": f"配置已保存! Token: {config.token[:8]}..., SubToken: {config.subtoken[:8]}..."
    }


@app.get("/api/system/auto-booking-config", tags=["系统配置"])
async def get_auto_booking_config(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    """获取自动补票配置"""
    config = auto_booking_service.get_config(db)
    return config


@app.post("/api/system/auto-booking-config", tags=["系统配置"])
async def set_auto_booking_config(
    config: AutoBookingConfig,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    """设置自动补票配置"""
    config_dict = config.dict()
    auto_booking_service.set_config(db, config_dict)
    
    # 如果启用，启动监控；如果禁用，停止监控
    if config.enabled:
        auto_booking_service.start_monitoring()
    else:
        auto_booking_service.stop_monitoring()
    
    return {"success": True, "message": "自动补票配置已保存"}


@app.get("/api/system/auto-booking-stats", tags=["系统配置"])
async def get_auto_booking_stats(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    """获取自动补票统计信息"""
    today_booked = auto_booking_service.get_today_booked_count(db)
    config = auto_booking_service.get_config(db)
    daily_target = config.get("daily_target", 0)
    available_accounts = len(auto_booking_service.get_available_accounts(db, limit=100))
    
    return {
        "today_booked": today_booked,
        "daily_target": daily_target,
        "remaining_target": max(0, daily_target - today_booked),
        "available_accounts": available_accounts,
        "is_monitoring": auto_booking_service.running
    }


@app.get("/api/system/refresh-time", tags=["系统配置"])
async def get_refresh_time(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    """获取每日刷新时间"""
    config = db.query(SystemConfig).filter(SystemConfig.key == "daily_refresh_time").first()
    return {"time": config.value if config else "06:00:00"}


@app.post("/api/system/refresh-time", tags=["系统配置"])
async def set_refresh_time(
    time_str: str,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    """设置每日刷新时间"""
    config = db.query(SystemConfig).filter(SystemConfig.key == "daily_refresh_time").first()
    if config:
        config.value = time_str
    else:
        config = SystemConfig(key="daily_refresh_time", value=time_str, description="每日Token刷新时间")
        db.add(config)
    db.commit()
    
    # 更新调度任务
    try:
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1])
        
        from apscheduler.triggers.cron import CronTrigger
        booking_scheduler.scheduler.reschedule_job(
            "daily_token_refresh",
            trigger=CronTrigger(hour=hour, minute=minute)
        )
    except Exception as e:
        logger.error(f"更新刷新时间失败: {e}")
    
    return {"success": True, "message": f"刷新时间已设置为 {time_str}"}


@app.post("/api/system/refresh-now", tags=["系统配置"])
async def refresh_all_tokens_now(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    """立即刷新所有账号Token"""
    background_tasks.add_task(daily_refresh_all_tokens)
    return {"success": True, "message": "刷新任务已在后台启动"}


# ========== 用户管理（管理员） ==========
@app.get("/api/users", response_model=List[UserResponse], tags=["用户管理"])
async def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    """获取所有用户列表"""
    return db.query(User).all()


@app.post("/api/users", response_model=UserResponse, tags=["用户管理"])
async def create_user(
    user_data: UserCreate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    """创建新用户"""
    existing = db.query(User).filter(User.username == user_data.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")
    
    user = User(
        username=user_data.username,
        hashed_password=get_password_hash(user_data.password),
        is_admin=user_data.is_admin
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.put("/api/users/{user_id}", response_model=UserResponse, tags=["用户管理"])
async def update_user(
    user_id: int,
    user_data: UserUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    """更新用户信息"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    if user_data.password:
        user.hashed_password = get_password_hash(user_data.password)
    if user_data.is_active is not None:
        user.is_active = user_data.is_active
    
    db.commit()
    db.refresh(user)
    return user


@app.delete("/api/users/{user_id}", tags=["用户管理"])
async def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_admin_user)
):
    """删除用户"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    
    db.delete(user)
    db.commit()
    return {"message": "删除成功"}


# ========== 预约账号管理 ==========
@app.get("/api/accounts", tags=["预约账号"])
async def list_accounts(
    page: int = 1,
    page_size: int = 20,
    search: Optional[str] = None,
    only_success: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """获取预约账号列表（支持分页和筛选）"""
    from math import ceil
    from sqlalchemy import or_
    
    # 构建基础查询
    if current_user.is_admin:
        query = db.query(BookingAccount)
    else:
        query = db.query(BookingAccount).filter(BookingAccount.owner_id == current_user.id)
    
    # 筛选：只看成功预约的账号
    if only_success:
        query = query.filter(BookingAccount.booking_success_time.isnot(None))
    
    # 筛选：按名字或邮箱搜索
    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            or_(
                BookingAccount.name.like(search_pattern),
                BookingAccount.email.like(search_pattern)
            )
        )
    
    # 获取总数
    total = query.count()
    
    # 分页
    offset = (page - 1) * page_size
    accounts = query.order_by(BookingAccount.created_at.desc()).offset(offset).limit(page_size).all()
    
    total_pages = ceil(total / page_size) if page_size > 0 else 0
    
    return {
        "items": accounts,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }


@app.post("/api/accounts", response_model=BookingAccountResponse, tags=["预约账号"])
async def create_account(
    account_data: BookingAccountCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """创建预约账号（自动创建邮箱并登录）"""
    account = BookingAccount(
        owner_id=current_user.id,
        name=account_data.name,
        email="",  # 将由自动登录填充
        id_card=account_data.id_card,
        companions=account_data.companions,
        entourage_list=[e.dict() for e in account_data.entourage_list],
        token_status="创建中..."
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    
    # 后台自动创建邮箱并登录
    def auto_login_task(account_id: int):
        task_db = SessionLocal()
        try:
            acc = task_db.query(BookingAccount).filter(BookingAccount.id == account_id).first()
            if acc:
                ok, msg = auto_create_email_and_login(task_db, acc)
                if not ok:
                    acc.token_status = f"登录失败: {msg[:20]}"
                    task_db.commit()
        finally:
            task_db.close()
    
    background_tasks.add_task(auto_login_task, account.id)
    
    return account


@app.get("/api/accounts/{account_id}", response_model=BookingAccountResponse, tags=["预约账号"])
async def get_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """获取预约账号详情"""
    account = db.query(BookingAccount).filter(BookingAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not current_user.is_admin and account.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问此账号")
    return account


@app.put("/api/accounts/{account_id}", response_model=BookingAccountResponse, tags=["预约账号"])
async def update_account(
    account_id: int,
    account_data: BookingAccountUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """更新预约账号"""
    account = db.query(BookingAccount).filter(BookingAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not current_user.is_admin and account.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权修改此账号")
    
    update_data = account_data.dict(exclude_unset=True)
    for key, value in update_data.items():
        if value is not None:
            if key == "entourage_list":
                value = [e if isinstance(e, dict) else e.dict() for e in value]
            setattr(account, key, value)
    
    db.commit()
    db.refresh(account)
    return account


@app.delete("/api/accounts/{account_id}", tags=["预约账号"])
async def delete_account(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """删除预约账号"""
    account = db.query(BookingAccount).filter(BookingAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not current_user.is_admin and account.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权删除此账号")
    
    db.delete(account)
    db.commit()
    return {"message": "删除成功"}


# ========== 账号Token操作 ==========
@app.post("/api/accounts/{account_id}/send-code", tags=["账号登录"])
async def send_verification_code(
    account_id: int,
    request: SendCodeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """发送港大验证码"""
    account = db.query(BookingAccount).filter(BookingAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    
    hku_api = HKUApiService()
    success, msg = hku_api.send_verification_code(request.email)
    
    if success:
        account.email = request.email
        db.commit()
        return {"success": True, "message": msg}
    else:
        raise HTTPException(status_code=400, detail=msg)


@app.post("/api/accounts/{account_id}/login", tags=["账号登录"])
async def login_hku(
    account_id: int,
    request: LoginRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """使用验证码登录港大系统"""
    account = db.query(BookingAccount).filter(BookingAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    
    hku_api = HKUApiService()
    token, name, id_card = hku_api.login(request.email, request.code)
    
    if token:
        account.hku_token = token
        account.email = request.email
        account.token_status = "已登录"
        db.commit()
        return {"success": True, "message": "登录成功", "token": token}
    else:
        raise HTTPException(status_code=400, detail="登录失败")


@app.post("/api/accounts/{account_id}/verify-token", tags=["账号登录"])
async def verify_account_token(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """验证账号Token状态"""
    account = db.query(BookingAccount).filter(BookingAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    
    if not account.hku_token:
        return {"valid": False, "message": "未登录"}
    
    hku_api = HKUApiService(account.hku_token)
    valid, msg = hku_api.verify_token()
    
    account.token_status = "已登录" if valid else "已过期"
    db.commit()
    
    return {"valid": valid, "message": msg}


@app.post("/api/accounts/{account_id}/kuku-config", tags=["账号登录"])
async def set_kuku_config(
    account_id: int,
    config: KukuMailConfig,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """设置临时邮箱配置"""
    account = db.query(BookingAccount).filter(BookingAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    
    account.kuku_token = config.token
    account.kuku_subtoken = config.subtoken
    account.kuku_cookie = config.cookie
    db.commit()
    
    return {"success": True, "message": "配置已保存"}


@app.post("/api/accounts/{account_id}/auto-login", tags=["账号登录"])
async def auto_login(
    account_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """使用全局邮箱配置自动创建邮箱并登录获取Token"""
    account = db.query(BookingAccount).filter(BookingAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    
    # 使用全局配置
    ok, msg = auto_create_email_and_login(db, account)
    
    if ok:
        return {
            "success": True,
            "message": msg,
            "email": account.email,
            "token": account.hku_token
        }
    else:
        raise HTTPException(status_code=400, detail=msg)


@app.get("/api/accounts/{account_id}/emails", tags=["账号邮件"])
async def get_account_emails(
    account_id: int,
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """获取账号的最近邮件列表"""
    account = db.query(BookingAccount).filter(BookingAccount.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    
    if not current_user.is_admin and account.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问此账号")
    
    if not account.temp_email:
        raise HTTPException(status_code=400, detail="账号未设置临时邮箱")
    
    # 获取全局邮箱配置
    config = get_global_kuku_config(db)
    if not config["cookie"] or not config["token"] or not config["subtoken"]:
        raise HTTPException(status_code=400, detail="邮箱配置不完整，请在系统设置中配置")
    
    try:
        from services.email_service import KukuMailService
        mail_service = KukuMailService(
            token=config["token"],
            subtoken=config["subtoken"],
            cookie=config["cookie"]
        )
        
        emails = mail_service.get_recent_emails(account.temp_email, limit=limit)
        
        # 获取每封邮件的详细内容
        result = []
        for email_info in emails:
            content = mail_service.get_email_content(email_info["id"], email_info["key"])
            result.append({
                **email_info,
                "content": content or ""
            })
        
        return {"emails": result}
    except Exception as e:
        logger.error(f"获取邮件列表异常: {e}")
        raise HTTPException(status_code=500, detail=f"获取邮件失败: {str(e)}")


# ========== 预约任务管理 ==========
@app.get("/api/tasks", tags=["预约任务"])
async def list_tasks(
    page: int = 1,
    page_size: int = 20,
    target_date: Optional[str] = None,
    status: Optional[str] = None,
    account_name: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """获取预约任务列表（支持分页和筛选）"""
    from math import ceil
    from sqlalchemy import or_
    
    # 构建基础查询
    if current_user.is_admin:
        query = db.query(BookingTask)
    else:
    # 获取用户的账号ID列表
        account_ids = [a.id for a in db.query(BookingAccount).filter(
            BookingAccount.owner_id == current_user.id
        ).all()]
        query = db.query(BookingTask).filter(BookingTask.account_id.in_(account_ids))
    
    # 应用筛选
    if target_date:
        query = query.filter(BookingTask.target_date == target_date)
    if status:
        query = query.filter(BookingTask.status == status)
    
    # ★ 账号名筛选（支持模糊匹配）★
    if account_name:
        account_ids_by_name = db.query(BookingAccount.id).filter(
            or_(
                BookingAccount.name.like(f"%{account_name}%"),
                BookingAccount.email.like(f"%{account_name}%")
            )
        ).all()
        account_ids_by_name = [aid[0] for aid in account_ids_by_name]
        if account_ids_by_name:
            query = query.filter(BookingTask.account_id.in_(account_ids_by_name))
        else:
            # 如果没有匹配的账号，返回空结果
            return {
                "items": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "total_pages": 0
            }
    
    # 获取总数
    total = query.count()
    
    # 分页
    offset = (page - 1) * page_size
    tasks = query.order_by(BookingTask.created_at.desc()).offset(offset).limit(page_size).all()
    
    total_pages = ceil(total / page_size) if page_size > 0 else 0
    
    return {
        "items": tasks,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }


@app.post("/api/tasks", response_model=BookingTaskResponse, tags=["预约任务"])
async def create_task(
    task_data: BookingTaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """创建预约任务（创建后自动调度）"""
    account = db.query(BookingAccount).filter(BookingAccount.id == task_data.account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not current_user.is_admin and account.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权为此账号创建任务")
    
    task = BookingTask(
        account_id=task_data.account_id,
        task_mode=task_data.task_mode,
        target_date=task_data.target_date,
        time_slot=task_data.time_slot,
        slot_id=task_data.slot_id,
        trigger_time=task_data.trigger_time,
        max_retries=task_data.max_retries,
        auto_login=task_data.auto_login
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    
    # 创建后自动调度到 APScheduler
    success, msg = booking_scheduler.schedule_task(task.id, task.trigger_time)
    if success:
        task.status = "scheduled"
        db.commit()
        db.refresh(task)
        logger.info(f"任务 {task.id} 创建并自动调度成功: {msg}")
    else:
        logger.warning(f"任务 {task.id} 创建成功但自动调度失败: {msg}")
    
    return task


@app.put("/api/tasks/{task_id}", response_model=BookingTaskResponse, tags=["预约任务"])
async def update_task(
    task_id: int,
    task_data: BookingTaskUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """更新预约任务"""
    task = db.query(BookingTask).filter(BookingTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    update_data = task_data.dict(exclude_unset=True)
    for key, value in update_data.items():
        if value is not None:
            setattr(task, key, value)
    
    db.commit()
    db.refresh(task)
    return task


@app.delete("/api/tasks/{task_id}", tags=["预约任务"])
async def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """删除预约任务"""
    task = db.query(BookingTask).filter(BookingTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    # 检查权限
    if not current_user.is_admin:
        account = db.query(BookingAccount).filter(BookingAccount.id == task.account_id).first()
        if not account or account.owner_id != current_user.id:
            raise HTTPException(status_code=403, detail="无权删除此任务")
    
    # 取消调度
    booking_scheduler.cancel_scheduled_task(task_id)
    
    # 先删除相关的任务日志（避免外键约束错误）
    db.query(TaskLog).filter(TaskLog.task_id == task_id).delete()
    
    # 删除任务
    db.delete(task)
    db.commit()
    return {"message": "删除成功"}


@app.post("/api/tasks/batch-delete", tags=["预约任务"])
async def batch_delete_tasks(
    task_ids: List[int],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """批量删除预约任务"""
    if not task_ids:
        raise HTTPException(status_code=400, detail="请选择要删除的任务")
    
    # 获取任务列表
    if current_user.is_admin:
        tasks = db.query(BookingTask).filter(BookingTask.id.in_(task_ids)).all()
    else:
        # 获取用户的账号ID列表
        account_ids = [a.id for a in db.query(BookingAccount).filter(
            BookingAccount.owner_id == current_user.id
        ).all()]
        tasks = db.query(BookingTask).filter(
            BookingTask.id.in_(task_ids),
            BookingTask.account_id.in_(account_ids)
        ).all()
    
    if not tasks:
        raise HTTPException(status_code=404, detail="未找到可删除的任务")
    
    deleted_count = 0
    for task in tasks:
        # 取消调度
        booking_scheduler.cancel_scheduled_task(task.id)
        
        # 删除相关的任务日志
        db.query(TaskLog).filter(TaskLog.task_id == task.id).delete()
        
        # 删除任务
        db.delete(task)
        deleted_count += 1
    
    db.commit()
    return {"message": f"成功删除 {deleted_count} 个任务", "deleted_count": deleted_count}


@app.post("/api/tasks/batch-delete-by-filter", tags=["预约任务"])
async def batch_delete_tasks_by_filter(
    req: TaskBatchDeleteByFilterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """按目标日期范围和状态批量删除任务"""
    start_date = (req.start_date or "").strip()
    end_date = (req.end_date or "").strip()
    status_value = (req.status or "").strip()

    if not start_date or not end_date or not status_value:
        raise HTTPException(status_code=400, detail="开始日期、结束日期和状态不能为空")
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")

    # 构建基础查询（仅取ID，避免一次性加载大量对象导致内存压力）
    if current_user.is_admin:
        base_query = db.query(BookingTask.id).filter(
            BookingTask.target_date >= start_date,
            BookingTask.target_date <= end_date,
            BookingTask.status == status_value
        )
    else:
        account_ids = [a.id for a in db.query(BookingAccount.id).filter(
            BookingAccount.owner_id == current_user.id
        ).all()]
        account_ids = [x[0] for x in account_ids]
        if not account_ids:
            raise HTTPException(status_code=404, detail="未找到符合条件的任务")
        base_query = db.query(BookingTask.id).filter(
            BookingTask.account_id.in_(account_ids),
            BookingTask.target_date >= start_date,
            BookingTask.target_date <= end_date,
            BookingTask.status == status_value
        )

    matched_count = base_query.count()
    if matched_count == 0:
        raise HTTPException(status_code=404, detail="未找到符合条件的任务")

    deleted_count = 0
    batch_size = 2000

    while True:
        id_rows = base_query.limit(batch_size).all()
        task_ids = [row[0] for row in id_rows]
        if not task_ids:
            break

        for task_id in task_ids:
            booking_scheduler.cancel_scheduled_task(task_id)

        db.query(TaskLog).filter(TaskLog.task_id.in_(task_ids)).delete(synchronize_session=False)
        deleted_in_batch = db.query(BookingTask).filter(BookingTask.id.in_(task_ids)).delete(synchronize_session=False)
        db.commit()
        deleted_count += deleted_in_batch or 0

    return {
        "message": f"成功删除 {deleted_count} 个任务（状态: {status_value}, 日期范围: {start_date} ~ {end_date}）",
        "deleted_count": deleted_count
    }


@app.post("/api/tasks/{task_id}/schedule", tags=["任务控制"])
async def schedule_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """调度任务在设定时间执行"""
    task = db.query(BookingTask).filter(BookingTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    success, msg = booking_scheduler.schedule_task(task_id, task.trigger_time)
    
    if success:
        task.status = "scheduled"
        db.commit()
        return {"success": True, "message": msg}
    else:
        raise HTTPException(status_code=400, detail=msg)


@app.post("/api/tasks/{task_id}/run", tags=["任务控制"])
async def run_task_now(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """立即执行任务"""
    task = db.query(BookingTask).filter(BookingTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    success, msg = booking_scheduler.run_task_now(task_id)
    
    if success:
        return {"success": True, "message": msg}
    else:
        raise HTTPException(status_code=400, detail=msg)


@app.post("/api/tasks/{task_id}/stop", tags=["任务控制"])
async def stop_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """停止任务"""
    task = db.query(BookingTask).filter(BookingTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    success, msg = booking_scheduler.stop_task(task_id)
    
    return {"success": success, "message": msg}


@app.post("/api/tasks/{task_id}/cancel-appointment", tags=["任务控制"])
async def cancel_appointment(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """取消该任务对应的预约"""
    task = db.query(BookingTask).filter(BookingTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    # 检查权限
    account = db.query(BookingAccount).filter(BookingAccount.id == task.account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="关联账号不存在")
    
    if not current_user.is_admin and account.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权操作此任务")
    
    if not account.hku_token:
        raise HTTPException(status_code=400, detail="账号未登录，无法取消预约")
    
    try:
        # 使用账号的token查找预约记录
        hku_api = HKUApiService(account.hku_token)
        appointment = hku_api.find_appointment_by_date(task.target_date)
        
        if not appointment:
            return {
                "success": False,
                "message": f"未找到 {task.target_date} 的有效预约记录"
            }
        
        appointment_id = appointment.get("id")
        if not appointment_id:
            return {
                "success": False,
                "message": "预约记录中没有ID字段"
            }
        
        # 调用取消预约API
        success, msg = hku_api.cancel_appointment(appointment_id)
        
        if success:
            # 更新任务状态
            task.status = "cancelled"
            task.result_message = f"预约已取消: {msg}"
            db.commit()
        
        return {
            "success": success,
            "message": msg,
            "appointment_id": appointment_id
        }
    except Exception as e:
        logger.error(f"取消预约异常: {e}")
        raise HTTPException(status_code=500, detail=f"取消预约失败: {str(e)}")


# ========== 港大API代理 ==========
@app.get("/api/available-dates", tags=["港大API"])
async def get_available_dates_by_account(
    account_id: int = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """获取可预约日期（支持指定账号或自动选择有效账号）"""
    if account_id:
        account = db.query(BookingAccount).filter(BookingAccount.id == account_id).first()
        if account and account.hku_token:
            hku_api = HKUApiService(account.hku_token)
            dates = hku_api.get_available_dates()
            if dates:
                return dates
    
    # 回退：获取任意一个有效token
    accounts = db.query(BookingAccount).filter(
        BookingAccount.hku_token.isnot(None),
        BookingAccount.token_status == "已登录"
    ).all()
    
    for account in accounts:
        hku_api = HKUApiService(account.hku_token)
        dates = hku_api.get_available_dates()
        if dates:
            return dates
    
    raise HTTPException(status_code=400, detail="没有可用的Token获取日期信息")


@app.get("/api/hku/available-dates", tags=["港大API"])
async def get_available_dates(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """获取可预约日期（兼容旧接口）"""
    accounts = db.query(BookingAccount).filter(
        BookingAccount.hku_token.isnot(None),
        BookingAccount.token_status == "已登录"
    ).all()
    
    for account in accounts:
        hku_api = HKUApiService(account.hku_token)
        dates = hku_api.get_available_dates()
        if dates:
            return {"dates": dates}
    
    raise HTTPException(status_code=400, detail="没有可用的Token获取日期信息")


@app.get("/api/booking-stats/by-date", tags=["预约统计"])
async def get_booking_stats_by_date(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """获取每一天的预约数量统计"""
    from services.auto_booking_service import auto_booking_service
    
    # 计算今天到两周后的所有日期
    today = datetime.now().date()
    stats = {}
    for i in range(14):  # 两周 = 14天
        date_str = (today + timedelta(days=i)).strftime("%Y-%m-%d")
        count = auto_booking_service.get_date_booked_count(db, date_str)
        stats[date_str] = count
    
    return stats


@app.get("/api/booked-accounts-by-date", tags=["预约统计"])
async def get_booked_accounts_by_date(
    target_date: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """获取指定日期已成功预约的账号列表（用于转让功能）"""
    # 查询该日期成功预约的任务，获取关联的账号
    tasks_with_accounts = db.query(BookingTask).join(BookingAccount).filter(
        BookingTask.target_date == target_date,
        BookingTask.status == "success"
    ).all()
    
    seen_account_ids = set()
    result = []
    for task in tasks_with_accounts:
        account = task.account
        if account and account.id not in seen_account_ids:
            seen_account_ids.add(account.id)
            result.append({
                "id": account.id,
                "name": account.name,
                "email": account.email,
                "id_card": account.id_card,
                "token_status": account.token_status,
                "is_auto_created": account.is_auto_created,
                "task_id": task.id,
                "time_slot": task.time_slot,
                "result_message": task.result_message
            })
    
    return result


@app.post("/api/transfer-appointment", tags=["预约转让"])
async def transfer_appointment(
    req: TransferAppointmentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """一键转让预约：取消原账号的预约，用新信息重新预约"""
    source_account_id = req.source_account_id
    target_date = req.target_date
    target_name = req.target_name
    target_id_card = req.target_id_card
    target_companions = req.target_companions
    try:
        # 1. 获取源账号
        source_account = db.query(BookingAccount).filter(BookingAccount.id == source_account_id).first()
        if not source_account or not source_account.hku_token:
            raise HTTPException(status_code=404, detail="源账号不存在或未登录")
        
        # 2. 获取该账号在该日期的预约记录（使用精确日期匹配）
        hku_api = HKUApiService(source_account.hku_token)
        target_appointment = hku_api.find_appointment_by_date(target_date)
        
        if not target_appointment:
            raise HTTPException(status_code=404, detail=f"未找到账号在 {target_date} 的预约记录")
        
        appointment_id = target_appointment.get("id")
        
        # 3. 取消原预约
        success, msg = hku_api.cancel_appointment(appointment_id)
        if not success:
            raise HTTPException(status_code=500, detail=f"取消预约失败: {msg}")
        
        # 等待一下，确保取消操作完成
        import time
        time.sleep(1)
        
        # 4. 获取该日期的可用slot信息
        available_dates = hku_api.get_available_dates()
        target_slot = None
        for day_data in available_dates:
            if day_data.get("date") == target_date:
                slots = day_data.get("slots", [])
                # 找到原预约的slot（通过start_time_slot匹配）
                original_slot_id = target_appointment.get("booking_rule_slot_id")
                for slot in slots:
                    if slot.get("id") == original_slot_id:
                        target_slot = slot
                        break
                if not target_slot and slots:
                    # 如果找不到原slot，使用第一个可用slot
                    target_slot = slots[0]
                break
        
        if not target_slot:
            raise HTTPException(status_code=404, detail=f"未找到 {target_date} 的可用时间段")
        
        slot_id = target_slot.get("id")
        
        # 5. 使用新信息重新预约
        # 创建一个临时账号用于预约（或者使用源账号但更新信息）
        # 这里我们直接使用源账号的token，但用新信息预约
        transfer_hku_api = HKUApiService(source_account.hku_token)
        
        # 准备随行人员列表
        entourage_list = []
        if target_companions > 0:
            for i in range(target_companions):
                entourage_list.append({"name": f"随行人员{i+1}"})
        
        success, result_msg = transfer_hku_api.book(
            name=target_name,
            id_card=target_id_card,
            target_date=target_date,
            slot_id=slot_id,
            companions=target_companions,
            entourage_list=entourage_list
        )
        
        if not success:
            raise HTTPException(status_code=500, detail=f"重新预约失败: {result_msg}")
        
        # 6. 更新源账号的信息（可选，如果需要保存新信息）
        # source_account.name = target_name
        # source_account.id_card = target_id_card
        # source_account.companions = target_companions
        # db.commit()
        
        return {
            "success": True,
            "message": f"转让成功: 已取消原预约并重新预约给 {target_name}",
            "appointment_id": appointment_id,
            "new_slot_id": slot_id
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"转让预约异常: {e}")
        raise HTTPException(status_code=500, detail=f"转让预约失败: {str(e)}")


# ========== 任务日志 ==========
@app.get("/api/tasks/{task_id}/logs", response_model=List[TaskLogResponse], tags=["任务日志"])
async def get_task_logs(
    task_id: int,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """获取任务日志"""
    logs = db.query(TaskLog).filter(TaskLog.task_id == task_id).order_by(
        TaskLog.created_at.desc()
    ).limit(limit).all()
    return logs


@app.get("/api/logs", tags=["任务日志"])
async def get_all_logs(
    page: int = 1,
    page_size: int = 50,
    level: Optional[str] = None,
    task_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user)
):
    """获取所有日志（管理员，支持分页和筛选）"""
    from math import ceil
    
    query = db.query(TaskLog)
    
    # 应用筛选
    if level:
        query = query.filter(TaskLog.level == level.upper())
    if task_id:
        query = query.filter(TaskLog.task_id == task_id)
    
    # 获取总数
    total = query.count()
    
    # 分页
    offset = (page - 1) * page_size
    logs = query.order_by(TaskLog.created_at.desc()).offset(offset).limit(page_size).all()
    
    total_pages = ceil(total / page_size) if page_size > 0 else 0
    
    return {
        "items": logs,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages
    }


def _parse_log_time_param(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """与库中 naive UTC 的 created_at 对齐，避免 aware/naive 比较问题。"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _clear_logs_execute(
    db: Session, parsed_start: Optional[datetime], parsed_end: Optional[datetime]
) -> int:
    """按批删除 task_logs，返回删除总行数。"""
    batch_size = 20000
    deleted_total = 0

    while True:
        if parsed_start is None and parsed_end is None:
            res = db.execute(text("DELETE FROM task_logs LIMIT :lim"), {"lim": batch_size})
        elif parsed_start is not None and parsed_end is not None:
            res = db.execute(
                text(
                    "DELETE FROM task_logs WHERE created_at >= :start "
                    "AND created_at <= :end LIMIT :lim"
                ),
                {"start": parsed_start, "end": parsed_end, "lim": batch_size},
            )
        elif parsed_start is not None:
            res = db.execute(
                text("DELETE FROM task_logs WHERE created_at >= :start LIMIT :lim"),
                {"start": parsed_start, "lim": batch_size},
            )
        else:
            res = db.execute(
                text("DELETE FROM task_logs WHERE created_at <= :end LIMIT :lim"),
                {"end": parsed_end, "lim": batch_size},
            )
        db.commit()
        n = res.rowcount or 0
        if n == 0:
            break
        deleted_total += n

    return deleted_total


def _clear_logs_response(
    parsed_start: Optional[datetime], parsed_end: Optional[datetime], deleted_total: int
) -> dict:
    if parsed_start or parsed_end:
        return {
            "success": True,
            "message": f"已删除指定时间范围内 {deleted_total} 条日志",
            "deleted_count": deleted_total,
        }
    return {
        "success": True,
        "message": f"已清空全部日志，共删除 {deleted_total} 条",
        "deleted_count": deleted_total,
    }


def clear_logs_impl(
    db: Session,
    start_time: Optional[str],
    end_time: Optional[str],
) -> dict:
    """清空系统日志：解析参数、校验、执行分批删除；数据库异常转为 500 并带明细。"""
    try:
        parsed_start = _naive_utc(_parse_log_time_param(start_time))
        parsed_end = _naive_utc(_parse_log_time_param(end_time))
    except ValueError:
        raise HTTPException(status_code=400, detail="时间格式错误，请使用 ISO 格式")

    if parsed_start and parsed_end and parsed_start > parsed_end:
        raise HTTPException(status_code=400, detail="开始时间不能晚于结束时间")

    try:
        deleted_total = _clear_logs_execute(db, parsed_start, parsed_end)
    except SQLAlchemyError as e:
        logger.exception("清空任务日志失败（数据库）")
        try:
            db.rollback()
        except Exception:
            pass
        err_text = str(e)
        if getattr(e, "orig", None) is not None:
            err_text = str(e.orig)
        raise HTTPException(status_code=500, detail=f"数据库错误: {err_text}")

    return _clear_logs_response(parsed_start, parsed_end, deleted_total)


@app.delete("/api/logs", tags=["任务日志"])
async def clear_logs(
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user)
):
    """清空系统日志（兼容旧版 DELETE + Query 参数）。大数据量按批删除。"""
    _ = current_user
    return clear_logs_impl(db, start_time, end_time)


@app.post("/api/logs/clear", tags=["任务日志"])
async def clear_logs_post(
    body: ClearLogsRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user)
):
    """清空系统日志（推荐使用 POST + JSON，避免部分环境对 DELETE 带参支持不佳）。"""
    _ = current_user
    return clear_logs_impl(db, body.start_time, body.end_time)


@app.post("/api/tasks/verify-success-emails", tags=["预约验证"])
async def verify_success_emails(
    target_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """核对预约成功的任务是否有对应的成功邮件"""
    from services.email_service import KukuMailService
    from services.scheduler_service import booking_scheduler
    
    # 构建查询：查找所有状态为success的任务
    query = db.query(BookingTask).filter(BookingTask.status == "success")
    
    if target_date:
        query = query.filter(BookingTask.target_date == target_date)
    
    # 权限过滤
    if not current_user.is_admin:
        account_ids = [a.id for a in db.query(BookingAccount).filter(
            BookingAccount.owner_id == current_user.id
        ).all()]
        query = query.filter(BookingTask.account_id.in_(account_ids))
    
    success_tasks = query.all()
    
    if not success_tasks:
        return {
            "total": 0,
            "verified": 0,
            "missing_email": 0,
            "invalid_email": 0,
            "details": []
        }
    
    # 获取邮箱配置
    config = booking_scheduler.get_global_kuku_config(db)
    if not config["cookie"] or not config["token"] or not config["subtoken"]:
        raise HTTPException(status_code=400, detail="邮箱配置不完整，无法验证邮件")
    
    mail_service = KukuMailService(
        token=config["token"],
        subtoken=config["subtoken"],
        cookie=config["cookie"]
    )
    
    verified_count = 0
    missing_email_count = 0
    invalid_email_count = 0
    updated_to_failed_count = 0
    details = []
    
    for task in success_tasks:
        account = db.query(BookingAccount).filter(BookingAccount.id == task.account_id).first()
        if not account or not account.temp_email:
            missing_email_count += 1
            # ★ 将缺少邮件的任务状态改为失败 ★
            task.status = "failed"
            task.result_message = "核对失败：账号未设置临时邮箱，无预约成功邮件"
            updated_to_failed_count += 1
            
            # ★ 检查该账号是否还有其他成功的任务，如果没有则清除 booking_success_time ★
            if account:
                other_success_tasks = db.query(BookingTask).filter(
                    BookingTask.account_id == account.id,
                    BookingTask.id != task.id,
                    BookingTask.status == "success"
                ).count()
                if other_success_tasks == 0:
                    account.booking_success_time = None
            
            details.append({
                "task_id": task.id,
                "account_id": account.id if account else None,
                "account_name": account.name if account else "未知",
                "target_date": task.target_date,
                "status": "missing_email",
                "message": "账号未设置临时邮箱，已更新为失败",
                "updated": True
            })
            continue
        
        # 检查任务是否有确认邮件
        if not task.confirmation_email:
            missing_email_count += 1
            # ★ 将缺少邮件的任务状态改为失败 ★
            task.status = "failed"
            task.result_message = "核对失败：任务未保存确认邮件，无预约成功邮件"
            updated_to_failed_count += 1
            
            # ★ 检查该账号是否还有其他成功的任务，如果没有则清除 booking_success_time ★
            if account:
                other_success_tasks = db.query(BookingTask).filter(
                    BookingTask.account_id == account.id,
                    BookingTask.id != task.id,
                    BookingTask.status == "success"
                ).count()
                if other_success_tasks == 0:
                    account.booking_success_time = None
            
            details.append({
                "task_id": task.id,
                "account_id": account.id,
                "account_name": account.name,
                "target_date": task.target_date,
                "status": "missing_email",
                "message": "任务未保存确认邮件，已更新为失败",
                "updated": True
            })
            continue
        
        # 验证邮件内容是否为预约成功邮件
        is_valid = booking_scheduler._verify_success_email(task.confirmation_email, task.target_date)
        
        if is_valid:
            verified_count += 1
            details.append({
                "task_id": task.id,
                "account_id": account.id,
                "account_name": account.name,
                "target_date": task.target_date,
                "status": "verified",
                "message": "邮件验证通过",
                "updated": False
            })
        else:
            invalid_email_count += 1
            # ★ 将无效邮件的任务状态改为失败 ★
            task.status = "failed"
            task.result_message = "核对失败：邮件内容不包含预约成功信息"
            updated_to_failed_count += 1
            
            # ★ 检查该账号是否还有其他成功的任务，如果没有则清除 booking_success_time ★
            if account:
                other_success_tasks = db.query(BookingTask).filter(
                    BookingTask.account_id == account.id,
                    BookingTask.id != task.id,
                    BookingTask.status == "success"
                ).count()
                if other_success_tasks == 0:
                    account.booking_success_time = None
            
            details.append({
                "task_id": task.id,
                "account_id": account.id,
                "account_name": account.name,
                "target_date": task.target_date,
                "status": "invalid_email",
                "message": "邮件内容不包含预约成功信息，已更新为失败",
                "updated": True
            })
    
    # 提交所有状态更新
    db.commit()
    
    return {
        "total": len(success_tasks),
        "verified": verified_count,
        "missing_email": missing_email_count,
        "invalid_email": invalid_email_count,
        "updated_to_failed": updated_to_failed_count,
        "details": details
    }


@app.get("/api/tasks/{task_id}/email", tags=["任务邮件"])
async def get_task_email(
    task_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """获取任务的确认邮件内容"""
    task = db.query(BookingTask).filter(BookingTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    # 检查权限
    account = db.query(BookingAccount).filter(BookingAccount.id == task.account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="关联账号不存在")
    
    if not current_user.is_admin and account.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="无权访问此任务")
    
    if not task.confirmation_email:
        raise HTTPException(status_code=404, detail="该任务暂无确认邮件")
    
    return {
        "email": task.confirmation_email,
        "task_id": task.id,
        "target_date": task.target_date,
        "status": task.status
    }



# ========== 前端页面服务 ==========
from fastapi.staticfiles import StaticFiles
import os

def _get_frontend_dir() -> str:
    """动态定位前端构建目录，兼容源码运行与 PyInstaller 打包。"""
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        # 源码 / PyInstaller 单文件：backend/main.py -> ../frontend/dist
        os.path.join(os.path.dirname(base), "frontend", "dist"),
        # PyInstaller 单目录：_internal/main.py -> _internal/frontend/dist
        os.path.join(base, "frontend", "dist"),
    ]
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return candidates[0]


frontend_dir = _get_frontend_dir()

app.mount("/assets", StaticFiles(directory=os.path.join(frontend_dir, "assets")), name="assets")

@app.get("/", response_class=HTMLResponse, tags=["前端页面"])
async def serve_index():
    with open(os.path.join(frontend_dir, "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/{full_path:path}", response_class=HTMLResponse, tags=["前端页面"])
async def serve_spa(full_path: str):
    with open(os.path.join(frontend_dir, "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

def _open_browser_when_ready():
    """打包运行时自动打开浏览器。"""
    if not hasattr(sys, '_MEIPASS'):
        return

    def _open():
        time.sleep(2)
        webbrowser.open("http://localhost:5353")

    threading.Thread(target=_open, daemon=True).start()


if __name__ == "__main__":
    _open_browser_when_ready()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5353)



