"""Resolve Restic without allowing host PATH to override frozen distributions."""

import os
from pathlib import Path
import sys

from src.rclone_executable import resolve_rclone_executable


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


def restic_subprocess_environment(environment: dict[str, str]) -> dict[str, str]:
    """Copy an environment and expose bundled helpers to frozen Restic children."""
    prepared = dict(environment)
    if not getattr(sys, "frozen", False):
        return prepared

    # Validate the primary executable too: frozen mode must never silently use
    # host-installed tools if part of the bundle is incomplete. Rclone is a
    # Restic child process for rclone-backed repositories, so it must be present
    # before allowing Restic to inherit the bundle directory on PATH.
    resolve_restic_executable()
    resolve_rclone_executable()
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if not bundle_dir:
        raise FileNotFoundError("the frozen worker has no PyInstaller bundle directory for Restic")

    path_entries = [str(bundle_dir)]
    if prepared.get("PATH"):
        path_entries.append(prepared["PATH"])
    prepared["PATH"] = os.pathsep.join(path_entries)
    return prepared
