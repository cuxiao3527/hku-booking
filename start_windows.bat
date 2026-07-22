@echo off
chcp 65001 >nul
title 港大预约系统

echo ========================================
echo   港大预约系统 - 一键启动
echo ========================================
echo.

:: 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python
    echo 请先安装 Python 3.9+ 下载地址: https://www.python.org/downloads/
    echo 安装时务必勾选 "Add Python to PATH"
    pause
    exit /b 1
)

:: 安装依赖
echo [1/3] 安装 Python 依赖...
cd /d "%~dp0backend"
pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

:: 安装 Playwright（如果没装）
echo [2/3] 检查 Playwright...
pip show playwright >nul 2>&1
if %errorlevel% neq 0 (
    pip install playwright --quiet
    python -m playwright install chromium
)

:: 启动
echo [3/3] 启动服务器...
echo.
echo 浏览器打开后请访问: http://localhost:5353
echo 默认账号: admin / admin123
echo 关闭此窗口即可停止服务
echo.

python main.py

pause
