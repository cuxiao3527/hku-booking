@echo off
chcp 65001 >nul 2>&1
echo ========================================
echo   港大预约系统 - Windows 一键打包脚本
echo ========================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.9+
    echo 下载地址: https://www.python.org/downloads/
    echo 安装时务必勾选 "Add Python to PATH"
    pause
    exit /b 1
)

echo [1/5] 安装依赖...
cd /d "%~dp0backend"
pip install -r requirements.txt pyinstaller --quiet
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

echo [2/5] 清理旧构建...
if exist "dist" rmdir /s /q dist
if exist "build" rmdir /s /q build

echo [3/5] 开始打包（需要几分钟，请耐心等待）...
python -m PyInstaller hku-booking.spec --noconfirm --clean
if %errorlevel% neq 0 (
    echo [错误] 打包失败
    pause
    exit /b 1
)

echo [4/5] 复制可执行文件到根目录...
cd /d "%~dp0"
copy "backend\dist\HKUBookingWeb.exe" "港大预约系统.exe" >nul
echo   已生成: 港大预约系统.exe

echo [5/5] 创建启动说明...
(
echo 港大预约系统 - Windows 单文件版
echo ================================
echo.
echo 使用方法:
echo   双击 "港大预约系统.exe" 即可启动
echo   启动后浏览器会自动打开 http://localhost:5353
echo   关闭黑色命令窗口即可停止服务
echo.
echo 默认账号: admin / admin123
echo.
echo 注意: 首次启动可能稍慢，请耐心等待。
) > "启动说明.txt"

echo.
echo ========================================
echo   打包完成！
echo ========================================
echo.
echo   最终产物: 港大预约系统.exe
echo   位置: %%cd%%\港大预约系统.exe
echo.
echo   把这个 .exe 文件复制到任何 Windows
echo   电脑上双击即可使用，无需安装 Python。
echo.
pause
