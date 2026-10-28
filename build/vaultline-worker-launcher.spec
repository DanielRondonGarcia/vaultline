# -*- mode: python ; coding: utf-8 -*-
"""Small stable console launcher; the worker runtime is built separately."""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
LAUNCHER_VERSION = ROOT / "build/vaultline-worker-launcher.version"
if not LAUNCHER_VERSION.is_file():
    raise SystemExit(f"the launcher version file is missing: {LAUNCHER_VERSION}")

a = Analysis(
    [str(ROOT / "src/worker_agent/launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
    a.binaries,
    a.datas,
    [],
    name="vaultline-worker",
    version=str(LAUNCHER_VERSION),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
