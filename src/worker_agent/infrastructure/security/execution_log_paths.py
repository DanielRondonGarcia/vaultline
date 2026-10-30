"""Focused execution-log paths and retention-configuration component (S1, S3
in ``odd/tasks/persistent-worker-execution-logs.md``).

Contains the fixed host log location, the per-job fixed-path structure with
validated components and the default 30-day retention configuration; no
persistence, redaction, spawn or deletion logic lives here, so the sibling
``execution_log_capture`` module stays the single capture entrypoint (script
and frozen dispatch, Docker and stdin modes) and this slice stays a small,
self-contained module that imports nothing from the capture module or the
Docker SDK.

Fixed structure (S1, shared by Docker and native runtimes, S2):
``<root>/<worker>/<runtime>/<yyyymmdd>/<job>-<epoch>.log`` with a validated
worker and job component, where the root itself is the fixed host location
(or the ``WORKER_EXECUTION_LOG_ROOT`` override). Retention configuration
(S3) never raises: the default is 30 days and any override value is clamped
to the supported 1..365 window.

Path-shape validation feeding retention: only the exact managed layout is
recognised as managed, so the retention sweep in the capture module can
never delete anything outside the naming and directory layout this
component produces, no matter what root value the environment carries.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple


class ExecutionLogPaths:
    """Fixed host location, fixed per-job log paths, managed-layout shape
    validation and the default retention window."""

    ROOT_ENV = "WORKER_EXECUTION_LOG_ROOT"
    RETENTION_ENV = "WORKER_EXECUTION_LOG_RETENTION_DAYS"
    RUNTIME_KINDS = frozenset({"docker", "native"})
    # S3: the default retention window the user confirmed («30 días (recomendado)»).
    DEFAULT_RETENTION_DAYS = 30
    MIN_RETENTION_DAYS = 1
    MAX_RETENTION_DAYS = 365
    # Validated components keep the log path inside the fixed structure; the
    # same grammar pins the managed log file names below.
    _COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

    # Retention only ever removes managed job logs: the exact fixed layout
    # and file naming this module produces. Nothing else under the root is
    # touched, no matter what root value the environment carries.
    _DATE_DIRECTORY_PATTERN = re.compile(r"^\d{8}$")
    # Mirrors the component grammar's maximum so every name job_log_path
    # generates from an accepted job ID is recognised as managed (S3 retention
    # must be able to prune what this module creates).
    _LOG_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}-\d{10}(?:-\d{1,2})?\.log$")

    @classmethod
    def default_root(cls) -> str:
        override = os.environ.get(cls.ROOT_ENV)
        if isinstance(override, str) and override.strip():
            return override.strip()
        if os.name == "nt":
            program_data = os.environ.get("PROGRAMDATA") or "C:/ProgramData"
            return os.path.join(program_data, "docker-volume-backup", "execution-logs")
        return "/var/lib/docker-volume-backup/execution-logs"

    @classmethod
    def retention_days(cls) -> int:
        raw = os.environ.get(cls.RETENTION_ENV)
        if raw is None or not raw.strip():
            return cls.DEFAULT_RETENTION_DAYS
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return cls.DEFAULT_RETENTION_DAYS
        return min(cls.MAX_RETENTION_DAYS, max(cls.MIN_RETENTION_DAYS, value))

    @classmethod
    def component(cls, value: object, field: str) -> str:
        if not isinstance(value, str) or not cls._COMPONENT_PATTERN.fullmatch(value):
            raise ValueError(f"invalid execution log {field}")
        return value

    @classmethod
    def job_log_path(
        cls,
        root: object,
        worker_id: str,
        runtime_kind: str,
        job_id: str,
        *,
        when: Optional[datetime] = None,
    ) -> Path:
        """Return the fixed host path of a job log without creating it."""
        if runtime_kind not in cls.RUNTIME_KINDS:
            raise ValueError("invalid execution log runtime kind")
        worker = cls.component(worker_id, "worker ID")
        job = cls.component(job_id, "job ID")
        moment = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return Path(root) / worker / runtime_kind / moment.strftime("%Y%m%d") / f"{job}-{int(moment.timestamp())}.log"

    @classmethod
    def managed_directory_prefix(cls, parts: Tuple[str, ...]) -> bool:
        """Whether the relative directory parts match the managed worker,
        runtime and date layout (in order, at most three levels deep)."""
        if len(parts) < 1 or not cls._COMPONENT_PATTERN.fullmatch(parts[0]):
            return False
        if len(parts) >= 2 and parts[1] not in cls.RUNTIME_KINDS:
            return False
        if len(parts) >= 3 and not cls._DATE_DIRECTORY_PATTERN.fullmatch(parts[2]):
            return False
        return len(parts) <= 3

    @classmethod
    def managed_log_relative_parts(cls, parts: Tuple[str, ...]) -> bool:
        """Whether the relative parts name a managed job log file exactly."""
        if len(parts) != 4 or not cls.managed_directory_prefix(parts[:3]):
            return False
        return bool(cls._LOG_NAME_PATTERN.fullmatch(parts[3]))


__all__ = ["ExecutionLogPaths"]