# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for HKU Booking Web (v1.0.0)"""
import os
import sys
import playwright

backend_dir = os.path.abspath(SPECPATH)
frontend_dist = os.path.join(os.path.dirname(backend_dir), "frontend", "dist")

# Frontend static files
added_files = [
    (frontend_dist, "frontend/dist"),
]

# 打包 kuku_config.json（首次启动自动加载邮箱配置）
_kuku_cfg = os.path.join(backend_dir, "kuku_config.json")
if os.path.exists(_kuku_cfg):
    added_files.append((_kuku_cfg, "."))
    print(f"[spec] Kuku config bundled")


# Playwright Node.js driver
playwright_dir = os.path.dirname(playwright.__file__)
driver_dir = os.path.join(playwright_dir, "driver")
if os.path.exists(driver_dir):
    added_files.append((driver_dir, "playwright/driver"))
    print(f"[spec] Playwright driver: {driver_dir}")

# Chromium browser - find and bundle
chromium_path = os.environ.get("CHROMIUM_PATH", "")
if chromium_path and os.path.exists(chromium_path):
    # chromium_path is a file path, bundle its parent directory
    chrome_dir = os.path.dirname(chromium_path)  # .../chrome-win64
    added_files.append((chrome_dir, "chromium"))
    print(f"[spec] Chromium bundled: {chromium_path}")
else:
    # Fallback: search Playwright cache
    cache_base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if not cache_base:
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            cache_base = os.path.join(userprofile, "AppData", "Local", "ms-playwright")
    if os.path.exists(cache_base):
        for item in os.listdir(cache_base):
            if item.startswith("chromium-"):
                for subdir in ["chrome-win64", "chrome-win"]:
                    exe_path = os.path.join(cache_base, item, subdir, "chrome.exe")
                    if os.path.exists(exe_path):
                        chrome_dir = os.path.dirname(exe_path)
                        added_files.append((chrome_dir, "chromium"))
                        print(f"[spec] Chromium fallback: {exe_path}")
                        break
                break
    print(f"[spec] Chromium NOT bundled (not found)")

a = Analysis(
    ["launcher.py"],
    pathex=[backend_dir],
    binaries=[],
    datas=added_files,
    hiddenimports=[
        "playwright",
        "playwright.sync_api",
        "playwright._impl._sync_base",
        "playwright._impl._connection",
        "playwright._impl._errors",
        "playwright._impl._browser_type",
        "playwright._impl._transport",
        "pyee",
        "pyee.u bridge",
        "greenlet",
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.config",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "passlib.handlers.bcrypt",
        "pydantic",
        "pydantic.deprecated.decorator",
        "sqlalchemy.sql.default_comparator",
        "apscheduler.schedulers.background",
        "apscheduler.triggers.cron",
        "apscheduler.triggers.interval",
        "apscheduler.executors.pool",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="HKUBookingWeb",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 无窗口静默运行
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="",
)
