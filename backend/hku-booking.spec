# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for HKU Booking Web - bundles Playwright driver + Chromium."""
import os
import sys
import playwright

backend_dir = os.path.abspath(SPECPATH)
frontend_dist = os.path.join(os.path.dirname(backend_dir), "frontend", "dist")

# 1. Frontend static files
added_files = [
    (frontend_dist, "frontend/dist"),
]

# 2. Playwright driver (Node.js binary + package files)
playwright_dir = os.path.dirname(playwright.__file__)
driver_dir = os.path.join(playwright_dir, "driver")
if os.path.exists(driver_dir):
    added_files.append((driver_dir, "playwright/driver"))

# 3. Chromium browser (from env var or default cache path)
chromium_path = os.environ.get("CHROMIUM_PATH", "")
if not chromium_path:
    # Try to find Chromium in Playwright cache
    cache_dir = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", 
        os.path.join(os.environ.get("USERPROFILE", "C:\\Users\\default"), 
                     "AppData", "Local", "ms-playwright"))
    if os.path.exists(cache_dir):
        for item in os.listdir(cache_dir):
            if item.startswith("chromium"):
                chrome_exe = os.path.join(cache_dir, item, "chrome-win", "chrome.exe")
                if os.path.exists(chrome_exe):
                    chromium_path = chrome_exe
                    break

a = Analysis(
    ["main.py"],
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
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="",
)
