"""Resolve Restic without allowing host PATH to override frozen distributions."""

import os
from pathlib import Path
import sys


def resolve_restic_executable() -> str:
    """Return the packaged executable when frozen, or ``restic`` in source mode.

    Source-mode subprocesses intentionally retain the normal PATH lookup. Frozen
    workers use only the Restic binary extracted by PyInstaller; a missing
    bundled file is an error and never falls back to the host PATH.
    """
    if not getattr(sys, "frozen", False):
        return "restic"

    bundle_dir = getattr(sys, "_MEIPASS", None)
    if not bundle_dir:
        raise FileNotFoundError("the frozen worker has no PyInstaller bundle directory for Restic")

    filename = "restic.exe" if sys.platform == "win32" else "restic"
    executable = Path(bundle_dir) / filename
    if not executable.is_file():
        raise FileNotFoundError(f"the frozen worker is missing its bundled Restic executable: {executable}")
    if sys.platform != "win32" and not os.access(executable, os.X_OK):
        raise PermissionError(f"the bundled Restic executable is not executable: {executable}")
    return str(executable)
