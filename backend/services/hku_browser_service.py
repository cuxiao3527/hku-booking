import os
import sys
"""
港大浏览器自动化服务 - 使用 Playwright 处理 reCAPTCHA
"""
import requests
import time
import logging
from typing import Optional, Tuple, List, Dict
try:
    from playwright.sync_api import sync_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False
    sync_playwright = None

from config import HKU_API_SEND_CODE, HKU_API_LOGIN
from services.email_service import KukuMailService

logger = logging.getLogger(__name__)

RECAPTCHA_SITE_KEY = "6LcpnTEtAAAAAJi5ZZvdEyXD06N-gWIPpE3w3RFw"


def get_recaptcha_token() -> Optional[str]:
    if not _HAS_PLAYWRIGHT:
        logger.error("Playwright 未安装，无法获取 reCAPTCHA 令牌")
        logger.error("请在终端运行: pip install playwright && playwright install chromium")
        return None
    """获取 reCAPTCHA v3 令牌（独立函数，不需要 KukuMailService）"""
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception:
                import os, sys as _sys2
                # PyInstaller 打包路径（Windows）
                if hasattr(_sys2, '_MEIPASS'):
                    _cp = os.path.join(_sys2._MEIPASS, "chromium", "chrome-win64", "chrome.exe")
                    if os.path.exists(_cp):
                        browser = p.chromium.launch(executable_path=_cp, headless=True)
                        logger.info(f"使用内置 Chromium: {_cp}")
                    else:
                        raise RuntimeError("内置 Chromium 未找到")
                else:
                    fallback = os.path.expanduser("~/Library/Caches/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-mac-arm64/chrome-headless-shell")
                    if os.path.exists(fallback):
                        browser = p.chromium.launch(executable_path=fallback, headless=True)
                    else:
                        raise RuntimeError("No Chromium found")
            page = browser.new_page()
            page.goto("https://tourist-registration-form.hku.hk/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
            token = page.evaluate("() => new Promise(r => grecaptcha.ready(() => grecaptcha.execute(\"6LcpnTEtAAAAAJi5ZZvdEyXD06N-gWIPpE3w3RFw\", {action: \"submit\"}).then(r)))")
            browser.close()
            logger.info(f"reCAPTCHA token obtained: {token[:20]}...")
            return token
    except Exception as e:
        logger.error(f"get recaptcha token failed: {e}")
        return None


class BrowserLoginService:
    """使用 Playwright 浏览器获取 reCAPTCHA 令牌，完成港大登录"""

    def __init__(self, kuku_service: KukuMailService):
        self.kuku = kuku_service

    def _get_recaptcha_token(self) -> Optional[str]:
        """通过 Playwright 打开页面获取 reCAPTCHA v3 令牌"""
        try:
            with sync_playwright() as p:
                try:
                    # Try default Playwright browser path first
                    browser = p.chromium.launch(headless=True)
                except Exception:
                    # Fallback to known headless shell path
                    import os, sys as _sys
                    shell_paths = [
                        os.path.expanduser("~/Library/Caches/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-mac-arm64/chrome-headless-shell"),
                        os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux-arm64/chrome-headless-shell"),
                    ]
                    # PyInstaller 打包路径（Windows）
                    if hasattr(_sys, '_MEIPASS'):
                        for _name in ["chrome-win64", "chrome-win"]:
                            _cp = os.path.join(_sys._MEIPASS, "chromium", _name, "chrome.exe")
                            shell_paths.append(_cp)
                    for _path in shell_paths:
                        if os.path.exists(_path):
                            browser = p.chromium.launch(executable_path=_path, headless=True)
                            logger.info(f"使用备用浏览器路径: {_path}")
                            break
                    else:
                        raise RuntimeError("未找到可用的 Chromium 浏览器，请运行: playwright install chromium")
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                )
                page = context.new_page()
                
                # 打开港大页面加载 reCAPTCHA
                page.goto("https://tourist-registration-form.hku.hk/", 
                         wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                
                # 执行 reCAPTCHA v3 获取令牌
                token = page.evaluate(f"""() => {{
                    return new Promise((resolve) => {{
                        grecaptcha.ready(() => {{
                            grecaptcha.execute("{RECAPTCHA_SITE_KEY}", {{action: "submit"}})
                                .then(resolve);
                        }});
                    }});
                }}""")
                
                browser.close()
                logger.info(f"reCAPTCHA 令牌已获取: {token[:20]}...")
                return token
                
        except Exception as e:
            logger.error(f"获取 reCAPTCHA 令牌失败: {e}")
            return None

    def send_verification_code(self, email: str) -> Tuple[bool, str]:
        """
        通过浏览器获取 reCAPTCHA 令牌后发送验证码
        
        返回: (success, message)
        """
        # 获取 reCAPTCHA 令牌
        token = self._get_recaptcha_token()
        if not token:
            return False, "获取 reCAPTCHA 令牌失败"
        
        # 发送验证码
        try:
            session = requests.Session()
            session.get("https://tourist-registration-form.hku.hk/", timeout=10)
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://tourist-registration-form.hku.hk/",
                "Origin": "https://tourist-registration-form.hku.hk",
                "Content-Type": "application/json",
            }

            resp = session.post(
                HKU_API_SEND_CODE,
                headers=headers,
                json={"email": email, "recaptchaToken": token},
                timeout=10
            )
            data = resp.json()

            if data.get("code") == 1000:
                logger.info(f"验证码已发送到 {email}")
                return True, "验证码已发送"
            else:
                msg = data.get("message", "发送失败")
                logger.error(f"发送验证码失败: {msg}")
                # Try without recaptchaToken field
                resp2 = session.post(
                    HKU_API_SEND_CODE,
                    headers=headers,
                    json={"email": email},
                    timeout=10
                )
                data2 = resp2.json()
                if data2.get("code") == 1000:
                    logger.info(f"验证码已发送到 {email} (无需 token)")
                    return True, "验证码已发送"
                return False, f"发送验证码失败: {msg}"
                
        except Exception as e:
            logger.error(f"发送验证码异常: {e}")
            return False, str(e)

    def login_with_code(self, email: str, code: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """使用邮箱和验证码登录（不需要 reCAPTCHA）"""
        try:
            session = requests.Session()
            session.get("https://tourist-registration-form.hku.hk/", timeout=10)
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://tourist-registration-form.hku.hk/",
                "Origin": "https://tourist-registration-form.hku.hk",
                "Content-Type": "application/json",
            }
            
            resp = session.post(
                HKU_API_LOGIN,
                headers=headers,
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

    def book_appointment(self, hku_api, name: str, id_card: str, target_date: str, 
                          slot_id: int, companions: int = 0, 
                          entourage_list: List[Dict] = None) -> Tuple[bool, str]:
        """
        获取 reCAPTCHA 令牌后提交预约
        
        :param hku_api: 已初始化的 HKUApiService（含有效 Token）
        :param name: 预约人姓名
        :param id_card: 证件后4位
        :param target_date: 预约日期 YYYY-MM-DD
        :param slot_id: Slot ID
        :param companions: 随行人数
        :param entourage_list: 随行人员列表
        :return: (success, message)
        """
        # 获取 reCAPTCHA 令牌
        token = self._get_recaptcha_token()
        if not token:
            return False, "获取 reCAPTCHA 令牌失败"
        
        # 使用 reCAPTCHA 令牌提交预约
        return hku_api.book(
            name=name,
            id_card=id_card,
            target_date=target_date,
            slot_id=slot_id,
            companions=companions,
            entourage_list=entourage_list or [],
            recaptcha_token=token
        )

    def auto_login(self, email: str) -> Tuple[bool, str, Optional[str]]:
        """
        完整自动登录流程:
        1. 通过浏览器发送验证码
        2. 等待并读取邮箱中的验证码
        3. 使用验证码登录获取 Token
        
        返回: (success, message, token)
        """
        # Step 1: 发送验证码
        ok, msg = self.send_verification_code(email)
        if not ok:
            return False, msg, None
        
        # Step 2: 等待验证码邮件
        logger.info(f"等待验证码邮件发送到 {email}...")
        code = self.kuku.get_verification_code(email, timeout=120)
        if not code:
            return False, "未收到验证码邮件", None
        
        logger.info(f"收到验证码: {code}")
        
        # Step 3: 登录
        token, name, id_card = self.login_with_code(email, code)
        if token:
            return True, f"登录成功", token
        else:
            return False, "登录失败", None
