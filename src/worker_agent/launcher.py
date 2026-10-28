"""Stable console-owning entrypoint for the replaceable Windows worker runtime."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from src.worker_agent.update import UpdateError, run_worker_update

_RUNTIME_NAME = "vaultline-worker-runtime.exe"
_VERSION_RE = re.compile(r"\bvaultline-worker(?:-runtime)?\s+(\d+\.\d+\.\d+)\b", re.IGNORECASE)


def _runtime_path() -> Path:
    return Path(sys.executable).resolve().with_name(_RUNTIME_NAME)


def _installed_runtime_version() -> str:
    runtime = _runtime_path()
    if not runtime.is_file():
        raise UpdateError(f"installed worker runtime was not found at {runtime}")
    try:
        completed = subprocess.run(
            [str(runtime), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise UpdateError(f"could not read the installed worker version: {exc}") from exc
    match = _VERSION_RE.search(completed.stdout or "")
    if completed.returncode != 0 or match is None:
        raise UpdateError("could not determine the installed worker runtime version")
    return match.group(1)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments == ["update"]:
            print("Checking the latest stable Vaultline Worker release...")
            message = run_worker_update(current_version=_installed_runtime_version())
            print(message)
            return 0

        runtime = _runtime_path()
        if not runtime.is_file():
            print(f"error: installed worker runtime was not found at {runtime}", file=sys.stderr)
            return 1
        return subprocess.run([str(runtime), *arguments], check=False).returncode
    except UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not start the worker runtime: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
