"""Bounded logical-size scan for explicitly selected native filesystem paths."""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


MAX_SCAN_ENTRIES = 100_000
MAX_SCAN_SECONDS = 30
MAX_SCAN_PATHS = 64
MAX_PATH_LENGTH = 4096
MAX_WORKER_INPUT_BYTES = 2 * 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 2 * 1024 * 1024


def scan_filesystem_size(paths: list[str], *, max_entries: int = MAX_SCAN_ENTRIES, timeout_seconds: int = MAX_SCAN_SECONDS) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "complete": True,
        "logical_bytes": 0,
        "file_count": 0,
        "directory_count": 0,
        "symlinks_skipped": 0,
        "roots": [],
    }
    seen: set[str] = set()
    entries = 0
    capped = False

    for root in paths:
        root_result = {"path": root, "status": "complete", "logical_bytes": 0, "file_count": 0, "directory_count": 0, "symlinks_skipped": 0}
        stack: list[Any] = [Path(root)]
        while stack:
            if entries >= max_entries or time.monotonic() - started >= timeout_seconds:
                capped = True
                root_result["status"] = "limit_reached"
                break
            current = stack.pop()
            if not isinstance(current, Path):
                try:
                    child = next(current)
                except StopIteration:
                    current.close()
                    continue
                except OSError:
                    current.close()
                    root_result["status"] = "inaccessible"
                    continue
                stack.extend((current, Path(child.path)))
                continue
            key = os.path.normcase(os.path.abspath(os.fspath(current)))
            if key in seen:
                continue
            seen.add(key)
            entries += 1
            try:
                info = os.lstat(current)
            except (OSError, ValueError):
                root_result["status"] = "inaccessible"
                continue
            is_reparse_point = bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            if stat.S_ISLNK(info.st_mode) or is_reparse_point:
                root_result["symlinks_skipped"] += 1
            elif stat.S_ISREG(info.st_mode):
                root_result["logical_bytes"] += info.st_size
                root_result["file_count"] += 1
            elif stat.S_ISDIR(info.st_mode):
                root_result["directory_count"] += 1
                try:
                    stack.append(os.scandir(current))
                except OSError:
                    root_result["status"] = "inaccessible"
        for pending in stack:
            if not isinstance(pending, Path):
                pending.close()
        for key in ("logical_bytes", "file_count", "directory_count", "symlinks_skipped"):
            result[key] += root_result[key]
        if root_result["status"] != "complete":
            result["complete"] = False
        result["roots"].append(root_result)
        if capped:
            break
    if len(result["roots"]) < len(paths):
        result["complete"] = False
        result["reason"] = "scan_limit_reached"
        result["roots"].extend({"path": path, "status": "not_scanned", "logical_bytes": 0, "file_count": 0, "directory_count": 0, "symlinks_skipped": 0} for path in paths[len(result["roots"]):])
    elif not result["complete"]:
        result["reason"] = "scan_limit_reached" if capped else "some_paths_inaccessible"
    return result


def _incomplete_result(paths: list[str], reason: str) -> dict[str, Any]:
    return {
        "complete": False,
        "logical_bytes": 0,
        "file_count": 0,
        "directory_count": 0,
        "symlinks_skipped": 0,
        "reason": reason,
        "roots": [
            {"path": path, "status": "not_scanned", "logical_bytes": 0, "file_count": 0,
             "directory_count": 0, "symlinks_skipped": 0}
            for path in paths
        ],
    }


def _valid_paths(paths: Any) -> bool:
    return isinstance(paths, list) and 0 < len(paths) <= MAX_SCAN_PATHS and all(
        isinstance(path, str) and len(path) <= MAX_PATH_LENGTH for path in paths
    )


def estimate_filesystem_size_bounded(
    paths: list[str], *, timeout_seconds: float = MAX_SCAN_SECONDS
) -> dict[str, Any]:
    """Run the scan in a killable process so stalled filesystem calls are bounded."""
    if not _valid_paths(paths):
        return _incomplete_result([], "scan_failed")
    payload = json.dumps({"paths": paths}, ensure_ascii=True, separators=(",", ":"))
    if len(payload.encode("ascii")) > MAX_WORKER_INPUT_BYTES:
        return _incomplete_result(paths, "scan_failed")
    command = ([sys.executable, "__filesystem-size-worker"] if getattr(sys, "frozen", False) else
               [sys.executable, "-m", "src.worker_agent.cli", "__filesystem-size-worker"])
    kwargs: dict[str, Any] = {
        "input": payload,
        "timeout": timeout_seconds,
        "capture_output": True,
        "text": True,
        "shell": False,
    }
    if not getattr(sys, "frozen", False):
        kwargs["cwd"] = Path(__file__).resolve().parents[3]
    try:
        completed = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired:
        return _incomplete_result(paths, "scan_timeout")
    except (OSError, ValueError, subprocess.SubprocessError):
        return _incomplete_result(paths, "scan_failed")
    output = completed.stdout
    if completed.returncode != 0 or not isinstance(output, str) or len(output.encode("utf-8")) > MAX_WORKER_OUTPUT_BYTES:
        return _incomplete_result(paths, "scan_failed")
    try:
        result = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return _incomplete_result(paths, "scan_failed")
    if not isinstance(result, dict) or not isinstance(result.get("complete"), bool):
        return _incomplete_result(paths, "scan_failed")
    roots = result.get("roots")
    integer_fields = ("logical_bytes", "file_count", "directory_count", "symlinks_skipped")
    if (not isinstance(roots, list) or len(roots) != len(paths)
            or any(not isinstance(roots[index], dict) or roots[index].get("path") != path for index, path in enumerate(paths))
            or any(not isinstance(result.get(field), int) or result[field] < 0 for field in integer_fields)):
        return _incomplete_result(paths, "scan_failed")
    return result


def estimate_filesystem_size(paths: list[str], *, max_entries: int = MAX_SCAN_ENTRIES,
                             timeout_seconds: int = MAX_SCAN_SECONDS) -> dict[str, Any]:
    """Compatibility name for direct scanner callers; production uses bounded wrapper."""
    return scan_filesystem_size(paths, max_entries=max_entries, timeout_seconds=timeout_seconds)
