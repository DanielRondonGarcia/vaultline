"""Resolve Restic without allowing host PATH to override frozen distributions."""

import os
from pathlib import Path
import sys

from src.native_tool_executable import resolve_native_tool
from src.rclone_executable import resolve_rclone_executable


def resolve_restic_executable() -> str:
    """Return the installed or bundled Restic in frozen mode, or ``restic`` in source mode."""
    return resolve_native_tool("restic", "Restic")


RESTIC_PROGRESS_FPS_DEFAULT = "1"


def restic_command_with_backend_options(command: list[str], environment: dict[str, str]) -> list[str]:
    """Select the packaged rclone explicitly for frozen rclone-backed Restic commands."""
    prepared = list(command)
    if not prepared or not getattr(sys, "frozen", False):
        return prepared
    repository = environment.get("RESTIC_REPOSITORY", "")
    if not isinstance(repository, str) or not repository.startswith("rclone:"):
        return prepared
    if any(
        argument == "-o" and index + 1 < len(prepared) and prepared[index + 1].startswith("rclone.program=")
        or argument.startswith("-o=rclone.program=")
        for index, argument in enumerate(prepared[1:], start=1)
    ):
        return prepared
    rclone_program = resolve_rclone_executable()
    if "\\" in rclone_program or any(character.isspace() for character in rclone_program):
        # Restic shell-splits rclone.program itself; quote Windows separators and spaces.
        rclone_program = f'"{rclone_program}"'
    return [prepared[0], "-o", f"rclone.program={rclone_program}", *prepared[1:]]


def restic_subprocess_environment(environment: dict[str, str]) -> dict[str, str]:
    """Copy an environment and expose the packaged rclone to frozen Restic children.

    Restic emits ``--json`` status records ten times per second by default
    (``internal/ui/progress.go`` in v0.19.1), which floods the worker's log tail
    and the progress pipe. ``RESTIC_PROGRESS_FPS`` also governs JSON output, so
    one status record per second is enough for live progress; operators may
    still override it.
    """
    prepared = dict(environment)
    prepared.setdefault("RESTIC_PROGRESS_FPS", RESTIC_PROGRESS_FPS_DEFAULT)
    if not getattr(sys, "frozen", False):
        return prepared

    # Validate the primary executable too: frozen mode must never silently use
    # host-installed tools if part of the distribution is incomplete. Restic
    # spawns rclone through exec.LookPath for rclone-backed repositories, so the
    # directory holding the rclone the resolver actually chose goes first on PATH.
    resolve_restic_executable()
    rclone_dir = str(Path(resolve_rclone_executable()).parent)

    path_entries = [rclone_dir]
    if prepared.get("PATH"):
        path_entries.append(prepared["PATH"])
    prepared["PATH"] = os.pathsep.join(path_entries)
    return prepared
