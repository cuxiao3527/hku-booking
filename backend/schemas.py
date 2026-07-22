from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


# ========== 用户相关 ==========
class UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserUpdate(BaseModel):
    password: Optional[str] = None
    is_active: Optional[bool] = None


class UserResponse(BaseModel):
    id: int
    username: str
    is_admin: bool
    is_active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


# ========== 预约账号相关 ==========
class EntourageInfo(BaseModel):
    name: str


class BookingAccountCreate(BaseModel):
    name: str
    email: Optional[str] = ""
    id_card: str = ""
    companions: int = 0
    entourage_list: List[EntourageInfo] = []


class BookingAccountUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    id_card: Optional[str] = None
    companions: Optional[int] = None
    entourage_list: Optional[List[EntourageInfo]] = None
    hku_token: Optional[str] = None
    token_status: Optional[str] = None
    kuku_token: Optional[str] = None
    kuku_subtoken: Optional[str] = None
    kuku_cookie: Optional[str] = None


class BookingAccountResponse(BaseModel):
    id: int
    owner_id: int
    name: str
    email: str
    id_card: str
    companions: int
    entourage_list: List[dict]
    hku_token: Optional[str]
    token_status: str
    temp_email: Optional[str]
    booking_success_time: Optional[datetime]
    created_at: datetime
    
    class Config:
        from_attributes = True


# ========== 预约任务相关 ==========
class BookingTaskCreate(BaseModel):
    account_id: int
    task_mode: str = "stable"  # stable(稳定模式) / rapid(极速模式)
    target_date: str
    time_slot: str = "AM"  # AM/PM
    slot_id: Optional[int] = None  # 极速模式下可指定slot_id
    trigger_time: str = "08:59:59"
    max_retries: int = 15
    auto_login: bool = True


class BookingTaskUpdate(BaseModel):
    task_mode: Optional[str] = None
    target_date: Optional[str] = None
    time_slot: Optional[str] = None
    slot_id: Optional[int] = None
    trigger_time: Optional[str] = None
    max_retries: Optional[int] = None
    auto_login: Optional[bool] = None
    status: Optional[str] = None


class TaskBatchDeleteByFilterRequest(BaseModel):
    start_date: str
    end_date: str
    status: str


class BookingTaskResponse(BaseModel):
    id: int
    account_id: int
    task_mode: str
    target_date: str
    time_slot: str
    slot_id: Optional[int]
    trigger_time: str
    max_retries: int
    status: str
    result_message: Optional[str]
    confirmation_email: Optional[str]
    auto_login: bool
    created_at: datetime
    executed_at: Optional[datetime]
    
    class Config:
        from_attributes = True


# ========== 临时邮箱相关 ==========
class KukuMailConfig(BaseModel):
    token: str       # csrf_token_check (32位)
    subtoken: str    # csrf_subtoken_check (32位)
    cookie: str      # 浏览器Cookie


class LoginRequest(BaseModel):
    email: str
    code: str


class SendCodeRequest(BaseModel):
    email: str


# ========== 港大API响应 ==========
class AvailableSlot(BaseModel):
    id: int
    start_time_slot: int
    quota: int


class AvailableDate(BaseModel):
    date: str
    slots: List[AvailableSlot]


# ========== 日志 ==========
class TaskLogResponse(BaseModel):
    id: int
    task_id: Optional[int]
    level: str
    message: str
    created_at: datetime
    
    class Config:
        from_attributes = True


class ClearLogsRequest(BaseModel):
    """清空任务日志（不传或传 null 表示不按该边界过滤）"""
    start_time: Optional[str] = None
    end_time: Optional[str] = None


# ========== 自动补票配置 ==========
class TransferAppointmentRequest(BaseModel):
    source_account_id: int
    target_date: str
    target_name: str
    target_id_card: str
    target_companions: int = 0


class AutoBookingConfig(BaseModel):
    enabled: bool = False  # 是否启用自动补票
    daily_target: int = 0  # 每天需要抢的名额数
    account_pool_size: int = 5  # 账号池大小（预先创建的账号数）
    monitor_interval: int = 30  # 监控间隔（秒）
    auto_create_account: bool = True  # 是否自动创建账号
    auto_create_task: bool = True  # 是否自动创建预约任务


# ========== 分页响应 ==========
class PaginatedResponse(BaseModel):
    items: List
    total: int
    page: int
    page_size: int
    total_pages: int

