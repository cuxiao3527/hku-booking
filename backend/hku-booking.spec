# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for HKU Booking Web (FastAPI + Vue3) - single .exe."""
import os

backend_dir = os.path.abspath(SPECPATH)
frontend_dist = os.path.join(os.path.dirname(backend_dir), "frontend", "dist")

added_files = [
    (frontend_dist, "frontend/dist"),
]

import os
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

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
        "pyee",
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
