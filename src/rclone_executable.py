"""Resolve rclone from frozen native distributions without host PATH fallback."""

from src.native_tool_executable import resolve_native_tool


def resolve_rclone_executable() -> str:
    """Return the installed or bundled rclone in frozen mode, or ``rclone`` in source mode."""
    return resolve_native_tool("rclone", "rclone")
