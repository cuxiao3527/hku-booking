from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
from database import Base


class User(Base):
    """系统用户（管理员/操作员）"""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True)
    hashed_password = Column(String(100))
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # 关联的预约账号
    booking_accounts = relationship("BookingAccount", back_populates="owner")


class BookingAccount(Base):
    """预约账号（用于港大系统的账号）"""
    __tablename__ = "booking_accounts"
    
    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"))
    
    # 基本信息
    name = Column(String(100))  # 预约者姓名
    email = Column(String(100))  # 邮箱
    id_card = Column(String(20))  # 证件后4位
    
    # 随行人员
    companions = Column(Integer, default=0)  # 随行人数
    entourage_list = Column(JSON, default=[])  # 随行人员列表
    
    # 港大系统Token
    hku_token = Column(Text, nullable=True)
    token_status = Column(String(20), default="未登录")  # 未登录/已登录/已过期
    
    # 临时邮箱配置
    temp_email = Column(String(100), nullable=True)
    kuku_token = Column(String(100), nullable=True)
    kuku_subtoken = Column(String(100), nullable=True)
    kuku_cookie = Column(Text, nullable=True)
    
    # 自动补票相关
    booking_success_time = Column(DateTime, nullable=True)  # 预约成功的时间
    is_auto_created = Column(Boolean, default=False)  # 是否为自动创建的账号（用于自动补票）
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    owner = relationship("User", back_populates="booking_accounts")
    booking_tasks = relationship("BookingTask", back_populates="account")


class BookingTask(Base):
    """预约任务"""
    __tablename__ = "booking_tasks"
    
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("booking_accounts.id"))
    
    # 任务模式: stable(稳定模式-先查询再预约) / rapid(极速模式-跳过查询直接打)
    task_mode = Column(String(10), default="stable")
    
    # 预约目标
    target_date = Column(String(20))  # 目标日期 YYYY-MM-DD
    time_slot = Column(String(10))  # AM/PM
    slot_id = Column(Integer, nullable=True)  # 时间段ID（极速模式必填）
    
    # 执行设置
    trigger_time = Column(String(20), default="08:59:59")  # 触发时间
    max_retries = Column(Integer, default=15)  # 最大重试次数
    
    # 状态
    status = Column(String(20), default="pending")  # pending/running/success/failed/cancelled
    result_message = Column(Text, nullable=True)
    
    # 预约确认邮件
    confirmation_email = Column(Text, nullable=True)  # 预约成功后的确认邮件原文
    
    # 自动登录配置
    auto_login = Column(Boolean, default=True)  # 是否自动登录获取Token
    
    created_at = Column(DateTime, default=datetime.utcnow)
    executed_at = Column(DateTime, nullable=True)
    
    account = relationship("BookingAccount", back_populates="booking_tasks")


class SystemConfig(Base):
    """系统配置"""
    __tablename__ = "system_config"
    
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(50), unique=True, index=True)
    value = Column(Text)
    description = Column(String(200), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TaskLog(Base):
    """任务日志"""
    __tablename__ = "task_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("booking_tasks.id"), nullable=True)
    level = Column(String(10), default="INFO")  # INFO/WARN/ERROR
    message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


