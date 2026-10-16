"""Locate Restic and rclone for frozen native workers without host PATH fallback.

Frozen workers prefer the copies installed at a stable path beside the worker
over the copies PyInstaller extracts into a fresh ``%TEMP%\\_MEIxxxx`` folder on
every start. Windows Defender's ``Behavior:Win32/SuspRclone.A`` detection
terminates an unsigned rclone that runs from a temporary path, and operators can
only exclude a path that does not change between runs. The extracted copy is
kept as the fallback for the portable executable.
"""

import os
from pathlib import Path
import sys


def installed_tool_directory() -> Path:
    """Return the stable directory where native packages install Restic and rclone.

    The Windows MSI installs both tools next to ``vaultline-worker.exe``
    (``C:\\Program Files\\Vaultline``). The Debian package keeps them out of
    ``/usr/bin`` so the worker never picks up a distro-installed tool with a
    different version: they live in ``<prefix>/lib/vaultline`` next to
    ``<prefix>/bin/vaultline-worker`` (``/usr/lib/vaultline`` for the .deb).
    """
    executable_dir = Path(sys.executable).resolve().parent
    if sys.platform == "win32":
        return executable_dir
    return executable_dir.parent / "lib" / "vaultline"


def frozen_tool_directories() -> list[Path]:
    """Return the directories searched for Restic and rclone in frozen mode, in order."""
    directories = [installed_tool_directory()]
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        directories.append(Path(bundle_dir))
    return directories


def resolve_native_tool(tool: str, display_name: str) -> str:
    """Return the installed or bundled ``tool`` when frozen, or ``tool`` in source mode.

    Source-mode subprocesses intentionally retain the normal PATH lookup. Frozen
    workers search only the installed directory and then the PyInstaller bundle;
    a missing executable is an error and never falls back to the host PATH.
    """
    if not getattr(sys, "frozen", False):
        return tool

    filename = f"{tool}.exe" if sys.platform == "win32" else tool
    searched = [directory / filename for directory in frozen_tool_directories()]
    for executable in searched:
        if not executable.is_file():
            continue
        if sys.platform != "win32" and not os.access(executable, os.X_OK):
            raise PermissionError(f"the {display_name} executable is not executable: {executable}")
        return str(executable)
    locations = ", ".join(str(path) for path in searched)
    raise FileNotFoundError(
        f"the frozen worker is missing its installed or bundled {display_name} executable (searched: {locations})"
    )
