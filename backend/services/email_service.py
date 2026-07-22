"""
临时邮箱服务 - 使用 kuku.lu 创建临时邮箱并获取验证码
"""
import requests
import re
import time
from bs4 import BeautifulSoup
from typing import Optional, Tuple, List, Dict
import logging

logger = logging.getLogger(__name__)


class KukuMailService:
    def __init__(self, token: str, subtoken: str, cookie: str):
        """
        初始化邮箱服务（与 email_login.py 保持一致）
        
        :param token: csrf_token_check (32位)
        :param subtoken: csrf_subtoken_check (32位)
        :param cookie: 浏览器 Cookie
        """
        self.session = requests.Session()
        self.base_url = "https://m.kuku.lu"
        self.token = token
        self.subtoken = subtoken
        self.cookie = cookie
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Origin': 'https://m.kuku.lu',
            'Referer': 'https://m.kuku.lu/',
            'X-Requested-With': 'XMLHttpRequest',
            'Cookie': cookie
        }
        
        self._last_token_refresh = 0
        self._token_refresh_interval = 300  # 5 minutes
        logger.info(f"邮箱服务初始化: Token={token[:8]}..., SubToken={subtoken[:8]}...")
    
    def _refresh_csrf_tokens(self) -> bool:
        """从 kuku.lu 页面获取最新的 CSRF SubToken（Token 不会变）"""
        now = time.time()
        if now - self._last_token_refresh < self._token_refresh_interval:
            return True
        try:
            # 必须使用 self.headers（包含 Cookie），否则 kuku.lu 会拒绝请求
            resp = self.session.get(
                f"{self.base_url}/",
                headers=self.headers,
                timeout=15
            )
            import re
            # CSRF SubToken 在页面 JavaScript 中，格式如: csrf_subtoken_check=xxxxxxxx...
            match = re.search(r'csrf_subtoken_check=([a-f0-9]+)', resp.text)
            if match:
                self.subtoken = match.group(1)
                self._last_token_refresh = now
                logger.info(f"CSRF SubToken 已刷新: {self.subtoken[:8]}...")
                return True
            else:
                logger.warning(f"未找到 CSRF SubToken (页面长度: {len(resp.text)})")
                return False
        except Exception as e:
            logger.warning(f"刷新 CSRF Token 失败: {e}")
            return False

    def create_email(self) -> Optional[str]:
        """创建新的临时邮箱"""
        self._refresh_csrf_tokens()
        logger.info(f"使用 Token [{self.token[:6]}...] 请求创建邮箱...")
        
        params = {
            "action": "addMailAddrByAuto",
            "nopost": "1",
            "by_system": "1",
            "csrf_token_check": self.token,
            "csrf_subtoken_check": self.subtoken,
            "_": int(time.time() * 1000)
        }
        
        try:
            resp = self.session.get(
                f"{self.base_url}/index.php", 
                params=params, 
                headers=self.headers,
                timeout=10
            )
            
            if resp.text.startswith("OK:"):
                email = resp.text.split(":")[1]
                logger.info(f"邮箱创建成功: {email}")
                return email
            else:
                logger.error(f"创建失败，服务器返回: {resp.text}")
                return None
        except Exception as e:
            logger.error(f"创建请求异常: {e}")
            return None
    
    def wait_for_email(self, email: str, timeout: int = 120) -> Tuple[Optional[str], Optional[str]]:
        """等待接收邮件，返回 (mail_id, mail_key)"""
        self._refresh_csrf_tokens()
        logger.info(f"开始监听 {email} 的收件箱 (超时: {timeout}s)...")
        logger.info(f"使用 token: {self.token[:8] if self.token else 'None'}..., subtoken: {self.subtoken[:8] if self.subtoken else 'None'}...")
        
        start_time = time.time()
        check_count = 0
        
        while time.time() - start_time < timeout:
            url = f"{self.base_url}/recv._ajax.php"
            params = {
                "q": email,
                "nopost": "1",
                "csrf_token_check": self.token,
                "csrf_subtoken_check": self.subtoken,
                "_": int(time.time() * 1000)
            }
            
            try:
                resp = self.session.get(url, params=params, headers=self.headers, timeout=10)
                check_count += 1
                
                # 调试：打印前几次响应
                if check_count <= 3:
                    logger.info(f"第{check_count}次检查，响应长度: {len(resp.text)}")
                    # 打印响应的前500个字符
                    logger.debug(f"响应内容预览: {resp.text[:500]}")
                
                # 检查响应是否包含错误
                if "error" in resp.text.lower() or "invalid" in resp.text.lower():
                    logger.warning(f"响应可能包含错误: {resp.text[:200]}")
                
                # 匹配邮件ID和Key
                match = re.search(r"openMailData\('(\d+)',\s*'([a-f0-9]+)'", resp.text)
                
                if match:
                    mail_id = match.group(1)
                    mail_key = match.group(2)
                    logger.info(f"收到新邮件！ID: {mail_id}, Key: {mail_key}")
                    return mail_id, mail_key
                
                # 尝试其他匹配模式
                if "area_mail_" in resp.text:
                    logger.info("检测到邮件列表区域，尝试其他匹配...")
                    # 尝试匹配 num 和 key 参数
                    alt_match = re.search(r"num=(\d+).*?key=([a-f0-9]+)", resp.text)
                    if alt_match:
                        logger.info(f"使用备用模式匹配到邮件")
                        return alt_match.group(1), alt_match.group(2)
                    
            except Exception as e:
                logger.error(f"监听出错: {e}")
            
            time.sleep(2)
        
        logger.warning(f"等待邮件超时，共检查 {check_count} 次")
        return None, None
    
    def get_email_content(self, mail_id: str, mail_key: str) -> Optional[str]:
        """获取邮件内容"""
        logger.info("正在读取邮件正文...")
        url = f"{self.base_url}/smphone.app.recv.view.php"
        data = {
            "num": mail_id,
            "key": mail_key,
            "noscroll": "1"
        }
        
        try:
            resp = self.session.post(url, data=data, headers=self.headers, timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            content_div = soup.find('div', id='area-data')
            if content_div:
                text = content_div.get_text(separator="\n", strip=True)
                logger.info(f"邮件内容已获取（{len(text)}字符）")
                return text
            else:
                logger.warning("未找到正文内容区域")
                return None
        except Exception as e:
            logger.error(f"获取正文失败: {e}")
            return None
    
    def extract_verification_code(self, content: str) -> Optional[str]:
        """
        从港大验证码邮件中提取验证码
        
        邮件格式示例：
        Dear User / 親愛的用戶 / 亲爱的用户,
        Your verification code for logging in the Tourist Registration System is as follows...
        6864
        Thank you!   謝謝！  谢谢！
        """
        if not content:
            return None
        
        # 查找4-6位数字验证码
        # 验证码通常在 "30 分钟内使用" 之后
        lines = content.split('\n')
        
        for i, line in enumerate(lines):
            # 找到包含 "30 分" 或 "30 minutes" 的行
            if "30 分" in line or "30 minutes" in line:
                # 检查下一行是否是纯数字
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if next_line.isdigit() and 4 <= len(next_line) <= 6:
                        logger.info(f"提取到验证码: {next_line}")
                        return next_line
        
        # 备用方案：查找独立的4-6位数字行
        for line in lines:
            line = line.strip()
            if line.isdigit() and 4 <= len(line) <= 6:
                logger.info(f"提取到验证码（备用方案）: {line}")
                return line
        
        logger.warning("未能提取验证码")
        return None
    
    def get_verification_code(self, email: str, timeout: int = 120) -> Optional[str]:
        """
        完整流程：等待邮件 -> 获取内容 -> 提取验证码
        """
        mail_id, mail_key = self.wait_for_email(email, timeout)
        if not mail_id or not mail_key:
            return None
        
        content = self.get_email_content(mail_id, mail_key)
        if not content:
            return None
        
        return self.extract_verification_code(content)
    
    def get_recent_emails(self, email: str, limit: int = 10) -> List[Dict[str, str]]:
        """
        获取最近的邮件列表
        返回: [{"id": mail_id, "key": mail_key, "from": from_addr, "subject": subject, "preview": preview}, ...]
        """
        url = f"{self.base_url}/recv._ajax.php"
        params = {
            "q": email,
            "nopost": "1",
            "csrf_token_check": self.token,
            "csrf_subtoken_check": self.subtoken,
            "_": int(time.time() * 1000)
        }
        
        try:
            resp = self.session.get(url, params=params, headers=self.headers, timeout=10)
            
            if resp.status_code != 200:
                logger.error(f"获取邮件列表失败: HTTP {resp.status_code}")
                return []
            
            # 解析HTML获取邮件列表
            soup = BeautifulSoup(resp.text, 'html.parser')
            emails = []
            
            # 方法1: 查找所有包含 openMailData 的链接或按钮
            # 匹配模式: openMailData('123', 'abc123def456')
            pattern = re.compile(r"openMailData\s*\(\s*['\"](\d+)['\"]\s*,\s*['\"]([a-f0-9]+)['\"]")
            matches = pattern.findall(resp.text)
            
            for mail_id, mail_key in matches[:limit]:
                # 尝试从HTML中提取邮件信息
                # 查找包含这个mail_id的区域
                area_id = f"area_mail_{mail_id}"
                area = soup.find('div', id=area_id)
                
                from_addr = '未知发件人'
                subject = '无主题'
                preview = ''
                
                if area:
                    # 尝试提取发件人
                    from_elem = area.find(string=re.compile(r'From:|发件人:', re.I))
                    if from_elem:
                        parent = from_elem.find_parent()
                        if parent:
                            from_addr = parent.get_text(strip=True).replace('From:', '').replace('发件人:', '').strip()
                    
                    # 尝试提取主题
                    subject_elem = area.find(string=re.compile(r'Subject:|主题:', re.I))
                    if subject_elem:
                        parent = subject_elem.find_parent()
                        if parent:
                            subject = parent.get_text(strip=True).replace('Subject:', '').replace('主题:', '').strip()
                    
                    # 获取预览文本（前100字符）
                    text = area.get_text(strip=True)
                    preview = text[:100] if text else ''
                
                emails.append({
                    "id": mail_id,
                    "key": mail_key,
                    "from": from_addr,
                    "subject": subject,
                    "preview": preview
                })
            
            logger.info(f"获取到 {len(emails)} 封邮件")
            return emails
            
        except Exception as e:
            logger.error(f"获取邮件列表异常: {e}")
            return []

