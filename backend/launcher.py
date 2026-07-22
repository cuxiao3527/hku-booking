"""
港大预约系统 - 启动器
双击 .exe 时自动判断：服务已运行→打开浏览器，未运行→启动服务→打开浏览器
"""
import sys
import socket
import time
import threading
import webbrowser

PORT = 5353


def is_port_in_use(port: int) -> bool:
    """检查端口是否已被占用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


def open_browser():
    """等待服务启动后打开浏览器"""
    for _ in range(15):  # 最多等 15 秒
        if is_port_in_use(PORT):
            break
        time.sleep(1)
    webbrowser.open(f'http://127.0.0.1:{PORT}')


def start_server():
    """启动服务"""
    from main import app
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=PORT, log_level='error')


if __name__ == '__main__':
    if is_port_in_use(PORT):
        # 服务已在运行，直接打开浏览器后退出
        webbrowser.open(f'http://127.0.0.1:{PORT}')
        sys.exit(0)
    else:
        # 启动服务 + 后台线程打开浏览器
        t = threading.Thread(target=open_browser, daemon=True)
        t.start()
        start_server()
