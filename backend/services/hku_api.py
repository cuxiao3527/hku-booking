"""
港大预约API服务
"""
import requests
import json
import time
from typing import Optional, Tuple, List, Dict, Any
from datetime import datetime, timedelta
import logging

from config import (
    HKU_API_BASE,
    HKU_API_GET_DATES,
    HKU_API_BOOK,
    HKU_API_SEND_CODE,
    HKU_API_LOGIN,
    HKU_API_APPOINTMENT_LIST,
    HKU_API_CANCEL_APPOINTMENT
)

logger = logging.getLogger(__name__)


class HKUApiService:
    HEADERS_TEMPLATE = {
        "Connection": "keep-alive",
        "Origin": "https://tourist-registration-form.hku.hk",
        "Referer": "https://tourist-registration-form.hku.hk/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json"
    }
    
    LOGIN_HEADERS = {
        "Connection": "keep-alive",
        "Origin": "https://tourist-registration-form.hku.hk",
        "Referer": "https://tourist-registration-form.hku.hk/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Content-Type": "application/json"
    }
    
    def __init__(self, token: Optional[str] = None):
        self.token = token
        self.session = requests.Session()
        self._ensure_session()
    
    def _ensure_session(self):
        """获取 HKU 主站的 session cookie（_app_sess），新版 API 需要此 cookie"""
        try:
            resp = self.session.get(
                "https://tourist-registration-form.hku.hk/",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=15
            )
            if resp.status_code == 200:
                logger.info("HKU session cookie 已获取")
            else:
                logger.warning(f"获取 HKU session cookie 失败: HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"获取 HKU session cookie 异常: {e}")

    def _get_headers(self, token: Optional[str] = None) -> dict:
        headers = self.HEADERS_TEMPLATE.copy()
        t = token or self.token
        if t:
            headers["Authorization"] = t
        return headers
    
    def send_verification_code(self, email: str) -> Tuple[bool, str]:
        """发送验证码到邮箱"""
        try:
            resp = self.session.post(
                HKU_API_SEND_CODE,
                headers=self.LOGIN_HEADERS,
                json={"email": email},
                timeout=10
            )
            data = resp.json()
            
            if data.get("code") == 1000:
                logger.info(f"验证码已发送到 {email}")
                return True, "验证码已发送"
            else:
                msg = data.get("message", "发送失败")
                logger.error(f"发送验证码失败: {msg}")
                return False, msg
        except Exception as e:
            logger.error(f"发送验证码异常: {e}")
            return False, str(e)
    
    def login(self, email: str, code: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        使用邮箱和验证码登录
        返回: (token, name, id_card) 或 (None, None, None)
        """
        try:
            resp = self.session.post(
                HKU_API_LOGIN,
                headers=self.LOGIN_HEADERS,
                json={"email": email, "emailCode": code},
                timeout=10
            )
            data = resp.json()
            
            if data.get("code") == 1000:
                user_data = data.get("data", {})
                token = user_data.get("token", {}).get("token")
                name = user_data.get("name")
                id_card = user_data.get("idCard", "")
                logger.info(f"登录成功: {email}")
                return token, name, id_card
            else:
                logger.error(f"登录失败: {data.get('message')}")
                return None, None, None
        except Exception as e:
            logger.error(f"登录异常: {e}")
            return None, None, None
    
    def get_available_dates(self) -> List[Dict[str, Any]]:
        """获取可预约日期和时间段，并计算实际剩余名额"""
        if not self.token:
            logger.error("未设置Token，无法获取可用日期")
            return []
        
        try:
            headers = self._get_headers()
            resp = self.session.get(HKU_API_GET_DATES, headers=headers, timeout=10)
            
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                # 处理每个日期下的slot，计算实际剩余名额
                for day_data in data:
                    slots = day_data.get("slots", [])
                    for slot in slots:
                        quota = slot.get("quota", 0)  # 总名额
                        amount = slot.get("amount", 0)  # 已预约数量
                        
                        # 计算实际剩余名额
                        # 如果quota为负数或0，表示没有可用名额
                        if quota <= 0:
                            available_quota = 0
                        else:
                            # 剩余名额 = 总名额 - 已预约数量
                            available_quota = max(0, quota - amount)
                        
                        # 更新slot数据，添加计算后的剩余名额
                        slot["available_quota"] = available_quota
                        slot["original_quota"] = quota
                        slot["booked_amount"] = amount
                
                return data
            else:
                logger.error(f"获取日期失败: HTTP {resp.status_code}")
                return []
        except Exception as e:
            logger.error(f"获取日期异常: {e}")
            return []
    
    def find_slot(self, target_date: str, time_slot: str = "AM") -> Optional[Dict]:
        """
        查找指定日期和时段的slot，返回包含实际剩余名额的slot信息
        time_slot: "AM" (10:00, start_time_slot=20) 或 "PM" (14:30, start_time_slot=29)
        """
        dates = self.get_available_dates()
        wanted_start = 20 if time_slot == "AM" else 29
        
        for day_data in dates:
            if day_data.get("date") == target_date:
                for slot in day_data.get("slots", []):
                    if slot.get("start_time_slot") == wanted_start:
                        # 确保slot包含计算后的剩余名额
                        if "available_quota" not in slot:
                            quota = slot.get("quota", 0)
                            amount = slot.get("amount", 0)
                            if quota <= 0:
                                available_quota = 0
                            else:
                                available_quota = max(0, quota - amount)
                            slot["available_quota"] = available_quota
                            slot["original_quota"] = quota
                            slot["booked_amount"] = amount
                        return slot
        return None
    
    def book(
        self,
        name: str,
        id_card: str,
        target_date: str,
        slot_id: int,
        companions: int = 0,
        entourage_list: List[Dict] = None
    ) -> Tuple[bool, str]:
        """发起预约请求"""
        if not self.token:
            return False, "未设置Token"
        
        if entourage_list is None:
            entourage_list = []
        
        payload = {
            "username": name,
            "companions": str(companions),
            "idCard": id_card,
            "status": 1,
            "book_date": target_date,
            "booking_rule_slot_id": slot_id,
            "entourageInfo": json.dumps(entourage_list)
        }
        
        logger.info(f"预约请求 payload: {json.dumps(payload, ensure_ascii=False)}")
        
        try:
            headers = self._get_headers()
            resp = self.session.post(
                HKU_API_BOOK,
                headers=headers,
                json=payload,
                timeout=3
            )
            
            logger.info(f"预约响应: status={resp.status_code}, body={resp.text[:500]}")
            
            if resp.status_code == 200:
                res = resp.json()
                code = res.get("code")
                msg = str(res.get("message", "")).lower()
                
                if code == 1000 and "fail" not in msg:
                    logger.info(f"预约成功: {name} - {target_date}")
                    return True, "预约成功"
                elif "repeated" in msg:
                    logger.info(f"重复预约: {name}")
                    return True, "已有预约记录"
                else:
                    logger.warning(f"预约失败: code={code}, message={res.get('message')}, 完整响应={res}")
                    return False, res.get("message", "预约失败")
            else:
                return False, f"HTTP {resp.status_code}"
        except Exception as e:
            logger.error(f"预约异常: {e}")
            return False, str(e)
    
    def verify_token(self) -> Tuple[bool, str]:
        """验证Token是否有效"""
        if not self.token:
            return False, "未设置Token"
        
        try:
            headers = self._get_headers()
            resp = self.session.post(
                HKU_API_APPOINTMENT_LIST,
                headers=headers,
                json={},
                timeout=5
            )
            
            if resp.status_code == 200:
                res = resp.json()
                if res.get("code") == 1000:
                    return True, "Token有效"
                elif res.get("code") in [401, 403] or "登录" in str(res.get("message", "")):
                    return False, "Token已失效"
                else:
                    return True, f"Token可用: {res.get('message', '')}"
            else:
                return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)
    
    def get_appointments(self) -> List[Dict]:
        """获取预约记录列表"""
        if not self.token:
            return []
        
        try:
            headers = self._get_headers()
            resp = self.session.post(
                HKU_API_APPOINTMENT_LIST,
                headers=headers,
                json={},
                timeout=10
            )
            
            if resp.status_code == 200:
                res = resp.json()
                if res.get("code") == 1000:
                    return res.get("data", [])
            return []
        except Exception as e:
            logger.error(f"获取预约记录异常: {e}")
            return []
    
    def verify_appointment(self, target_date: str) -> Tuple[bool, str]:
        """验证是否成功预约了指定日期"""
        appointments = self.get_appointments()
        
        # 标准化目标日期格式（确保是 YYYY-MM-DD）
        target_date_normalized = target_date.strip()
        if " " in target_date_normalized:
            target_date_normalized = target_date_normalized.split(" ")[0]
        if "T" in target_date_normalized:
            target_date_normalized = target_date_normalized.split("T")[0]
        
        for item in appointments:
            book_date = item.get("book_date", "")
            status = item.get("status")
            
            # 标准化预约日期格式
            book_date_normalized = book_date.strip()
            if " " in book_date_normalized:
                book_date_normalized = book_date_normalized.split(" ")[0]
            if "T" in book_date_normalized:
                book_date_normalized = book_date_normalized.split("T")[0]
            
            # 使用精确匹配而不是 in 操作
            if book_date_normalized == target_date_normalized and status == 1:
                return True, f"预约成功: {book_date}"
        
        return False, "未找到有效预约记录"

    def cancel_appointment(self, appointment_id: int) -> Tuple[bool, str]:
        """取消预约"""
        if not self.token:
            return False, "未设置Token"
        
        try:
            headers = self._get_headers()
            resp = self.session.post(
                HKU_API_CANCEL_APPOINTMENT,
                headers=headers,
                json={"id": appointment_id},
                timeout=10
            )
            
            if resp.status_code == 200:
                res = resp.json()
                if res.get("code") == 1000:
                    logger.info(f"取消预约成功: appointment_id={appointment_id}")
                    return True, "取消预约成功"
                else:
                    msg = res.get("message", "取消预约失败")
                    logger.error(f"取消预约失败: {msg}")
                    return False, msg
            else:
                return False, f"HTTP {resp.status_code}"
        except Exception as e:
            logger.error(f"取消预约异常: {e}")
            return False, str(e)
    
    def find_appointment_by_date(self, target_date: str) -> Optional[Dict]:
        """根据日期查找预约记录，返回预约记录信息"""
        appointments = self.get_appointments()
        
        if not appointments:
            logger.warning(f"获取预约列表为空，无法查找日期 {target_date} 的预约记录")
            return None
        
        # 标准化目标日期格式（确保是 YYYY-MM-DD）
        target_date_normalized = target_date.strip()
        if " " in target_date_normalized:
            target_date_normalized = target_date_normalized.split(" ")[0]
        if "T" in target_date_normalized:
            target_date_normalized = target_date_normalized.split("T")[0]
        
        logger.info(f"查找预约记录: 目标日期={target_date_normalized}, 预约列表数量={len(appointments)}")
        
        # 打印所有预约记录用于调试
        for idx, item in enumerate(appointments):
            book_date = item.get("book_date", "")
            status = item.get("status")
            appointment_id = item.get("id")
            logger.debug(f"预约记录[{idx}]: book_date={book_date}, status={status}, id={appointment_id}")
        
        for item in appointments:
            book_date = item.get("book_date", "")
            status = item.get("status")
            appointment_id = item.get("id")
            
            # 标准化预约日期格式
            book_date_normalized = book_date.strip()
            if " " in book_date_normalized:
                book_date_normalized = book_date_normalized.split(" ")[0]
            if "T" in book_date_normalized:
                book_date_normalized = book_date_normalized.split("T")[0]
            
            # 检查日期是否匹配
            date_matches = book_date_normalized == target_date_normalized
            
            # 优先查找 status == 1 的有效预约，如果没有则查找任何状态的预约（可能有些预约状态不是1但也能取消）
            if date_matches:
                if status == 1:
                    logger.info(f"找到匹配的有效预约记录: book_date={book_date} (标准化后={book_date_normalized}), status={status}, id={appointment_id}")
                    return item
                else:
                    logger.warning(f"找到日期匹配但状态不是1的预约记录: book_date={book_date}, status={status}, id={appointment_id}，仍返回该记录")
                    return item
        
        logger.warning(f"未找到匹配的预约记录: 目标日期={target_date_normalized}, 已检查 {len(appointments)} 条记录")
        # 打印所有预约记录的日期用于调试
        all_dates = [item.get("book_date", "") for item in appointments]
        logger.warning(f"所有预约记录的日期: {all_dates}")
        return None


