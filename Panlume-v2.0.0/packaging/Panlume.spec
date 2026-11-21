"""PyInstaller onedir build for Panlume. Run from the project root on Windows."""

import os
from pathlib import Path


PROJECT_ROOT = Path.cwd().resolve()
if not (PROJECT_ROOT / "desktop_launcher.py").is_file():
    raise RuntimeError("Panlume.spec must be run from the project root.")

DATAS = [
    (str(PROJECT_ROOT / "pandeng" / "storage" / "schema.sql"), "pandeng/storage"),
    (str(PROJECT_ROOT / "configs" / "analysis-test.yaml"), "configs"),
    (
        str(PROJECT_ROOT / "pandeng" / "desktop" / "assets"),
        "pandeng/desktop/assets",
    ),
]
BINARIES = []
HIDDENIMPORTS = []
if os.environ.get("PANDENG_INCLUDE_LIVE_DATA") == "1":
    from PyInstaller.utils.hooks import collect_all

    live_datas, live_binaries, live_hiddenimports = collect_all("akshare")
    DATAS += live_datas
    BINARIES += live_binaries
    HIDDENIMPORTS += live_hiddenimports

a = Analysis(
    [str(PROJECT_ROOT / "desktop_launcher.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=BINARIES,
    datas=DATAS,
    hiddenimports=HIDDENIMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Panlume",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "pandeng" / "desktop" / "assets" / "pandeng.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Panlume",
)
