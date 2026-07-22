import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def get_app_data_dir() -> str:
    """获取应用数据目录。

    开发/源码运行时：使用当前工作目录，便于调试。
    PyInstaller 打包后：
      - Windows / Linux：使用可执行文件所在目录。
      - macOS .app 包：使用 .app 所在文件夹（方便用户找到数据库）。
    """
    if hasattr(sys, '_MEIPASS'):
        exe = os.path.abspath(sys.executable)
        exe_dir = os.path.dirname(exe)
        # macOS .app 结构：App.app/Contents/MacOS/executable
        if sys.platform == "darwin" and ".app/Contents/MacOS" in exe_dir:
            app_bundle = os.path.dirname(os.path.dirname(exe_dir))
            return os.path.dirname(app_bundle)
        return exe_dir
    return os.getcwd()


APP_DATA_DIR = get_app_data_dir()
DB_PATH = os.path.join(APP_DATA_DIR, "hku_local.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

SECRET_KEY = os.environ.get("SECRET_KEY", "hku-booking-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7

HKU_API_BASE = "https://prod-app-finclip.azurewebsites.net"
HKU_API_GET_DATES = f"{HKU_API_BASE}/app/user/booking/availableDatesForIndividualTour"
HKU_API_BOOK = f"{HKU_API_BASE}/app/user/booking/applyViaH5"
HKU_API_SEND_CODE = f"{HKU_API_BASE}/app/user/login/emailCode"
HKU_API_LOGIN = f"{HKU_API_BASE}/app/user/login/email"
HKU_API_APPOINTMENT_LIST = f"{HKU_API_BASE}/app/user/login/getAppointmentList"
HKU_API_CANCEL_APPOINTMENT = f"{HKU_API_BASE}/app/user/login/cancelAppointment"

KUKU_MAIL_BASE = "https://m.kuku.lu"

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"
