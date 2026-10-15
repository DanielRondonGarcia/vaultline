# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file spec for the native Vaultline worker.

Build on each target OS/Python/architecture combination; one-file outputs are not
portable across operating systems or architectures.
"""

import os
import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH).resolve().parent
RESTIC_BINARY = Path(os.environ.get("VAULTLINE_RESTIC_BINARY", ""))
RESTIC_LICENSE = ROOT / "packaging/licenses/restic-v0.19.1-LICENSE"

if not RESTIC_BINARY.is_file():
    raise SystemExit("VAULTLINE_RESTIC_BINARY must point to the verified Restic release executable")
if not RESTIC_LICENSE.is_file():
    raise SystemExit(f"the bundled Restic license is missing: {RESTIC_LICENSE}")

hiddenimports = [
    "src.app.main",
    "src.app.domain.models",
    "src.app.application.services.backup_service",
    "src.app.application.services.restore_service",
    "src.app.infrastructure.adapters.backup_strategy",
    "src.app.infrastructure.adapters.storage.multi_storage_adapter",
    "src.app.infrastructure.adapters.notifier.influx_notifier",
    "src.worker_agent.cli",
    "src.worker_agent.main",
    "src.worker_agent.infrastructure.adapters.native_runtime",
    "src.worker_agent.infrastructure.api_client.control_plane_client",
    "src.worker_agent.infrastructure.security.credential_store",
    "src.worker_agent.windows_service",
]

if sys.platform == "win32":
    hiddenimports.extend([
        "backports",
        "backports.tarfile",
        # pywin32 modules imported lazily by src.worker_agent.windows_service so
        # the frozen exe can host the VaultlineWorker SCM service.
        "win32serviceutil",
        "win32service",
        "win32event",
        "servicemanager",
        "win32api",
        "win32timezone",
        "pywintypes",
        "win32con",
        "win32net",
        "win32netcon",
    ])


a = Analysis(
    [str(ROOT / "src/worker_agent/cli.py")],
    pathex=[str(ROOT)],
    binaries=[(str(RESTIC_BINARY), ".")],
    datas=[(str(RESTIC_LICENSE), "licenses/restic")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='vaultline-worker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
