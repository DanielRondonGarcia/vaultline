"""Resolve rclone from frozen native distributions without host PATH fallback."""

import os
from pathlib import Path
import sys


def resolve_rclone_executable() -> str:
    """Return bundled rclone in frozen mode, or ``rclone`` in source mode."""
    if not getattr(sys, "frozen", False):
        return "rclone"

    bundle_dir = getattr(sys, "_MEIPASS", None)
    if not bundle_dir:
        raise FileNotFoundError("the frozen worker has no PyInstaller bundle directory for rclone")

    filename = "rclone.exe" if sys.platform == "win32" else "rclone"
    executable = Path(bundle_dir) / filename
    if not executable.is_file():
        raise FileNotFoundError(f"the frozen worker is missing its bundled rclone executable: {executable}")
    if sys.platform != "win32" and not os.access(executable, os.X_OK):
        raise PermissionError(f"the bundled rclone executable is not executable: {executable}")
    return str(executable)
