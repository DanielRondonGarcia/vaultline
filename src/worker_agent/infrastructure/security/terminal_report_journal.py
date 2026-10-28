import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class WorkerTerminalReportJournal:
    """Atomic store for terminal job results the Control Plane has not accepted yet.

    A job that finished while the Control Plane was unreachable must not lose its
    result. The worker keeps the pending reports in memory and mirrors them here
    so a worker restart during the outage can still deliver them. Records are
    bounded: log lines are dropped oldest-first until the record fits.
    """

    VERSION = 1
    MAX_RECORDS = 32
    MAX_RECORD_BYTES = 2 * 1024 * 1024
    MAX_AGE_SECONDS = 7 * 24 * 60 * 60
    MAX_LOG_TIMESTAMP_CHARS = 64

    def __init__(self, path: str):
        self.path = Path(path)

    @classmethod
    def _bounded_log_timestamps(cls, log_lines: Any, log_timestamps: Any) -> Optional[List[Optional[str]]]:
        """Keep an optional per-line timestamp list only when it aligns 1:1 with lines."""
        if not isinstance(log_timestamps, list) or not isinstance(log_lines, list):
            return None
        lines = log_lines
        if len(log_timestamps) != len(lines):
            return None
        timestamps = [
            value[: cls.MAX_LOG_TIMESTAMP_CHARS] if isinstance(value, str) else None
            for value in log_timestamps
        ]
        return timestamps if any(value is not None for value in timestamps) else None

    @classmethod
    def bounded_record(
        cls,
        *,
        job_id: str,
        worker_id: str,
        lease_token: str,
        status: str,
        result_summary: Dict[str, Any],
        log_lines: List[str],
        log_timestamps: Optional[List[Optional[str]]] = None,
        created_at: Optional[str] = None,
        attempts: int = 0,
    ) -> Optional[Dict[str, Any]]:
        if not all(isinstance(value, str) and value for value in (job_id, worker_id, lease_token, status)):
            return None
        raw_lines = list(log_lines) if isinstance(log_lines, list) else []
        lines = [line for line in raw_lines if isinstance(line, str)]
        timestamps = (
            cls._bounded_log_timestamps(raw_lines, log_timestamps)
            if len(lines) == len(raw_lines)
            else None
        )
        record = {
            "version": cls.VERSION,
            "job_id": job_id,
            "worker_id": worker_id,
            "lease_token": lease_token,
            "status": status,
            "result_summary": result_summary if isinstance(result_summary, dict) else {},
            "log_lines": lines,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
            "attempts": int(attempts) if isinstance(attempts, int) and not isinstance(attempts, bool) else 0,
        }
        if timestamps is not None:
            record["log_timestamps"] = timestamps
        while True:
            try:
                encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=True)
            except (TypeError, ValueError):
                return None
            if len(encoded.encode("utf-8")) <= cls.MAX_RECORD_BYTES:
                return record
            if not record["log_lines"]:
                return None
            offset = max(1, len(record["log_lines"]) // 4)
            record["log_lines"] = record["log_lines"][offset:]
            if "log_timestamps" in record:
                record["log_timestamps"] = record["log_timestamps"][offset:]

    @classmethod
    def _normalize_record(cls, value: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(value, dict) or value.get("version") != cls.VERSION:
            return None
        created_at = value.get("created_at")
        if not isinstance(created_at, str) or len(created_at) > 64:
            return None
        try:
            timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
        if age > cls.MAX_AGE_SECONDS or age < -300:
            return None
        return cls.bounded_record(
            job_id=value.get("job_id"),
            worker_id=value.get("worker_id"),
            lease_token=value.get("lease_token"),
            status=value.get("status"),
            result_summary=value.get("result_summary"),
            log_lines=value.get("log_lines") if isinstance(value.get("log_lines"), list) else [],
            log_timestamps=value.get("log_timestamps") if isinstance(value.get("log_timestamps"), list) else None,
            created_at=created_at,
            attempts=value.get("attempts", 0),
        )

    def write(self, records: List[Dict[str, Any]]) -> None:
        bounded = [record for record in records if record is not None][-self.MAX_RECORDS :]
        if not bounded:
            self.clear()
            return
        encoded = json.dumps(bounded, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(parent))
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def load(self) -> List[Dict[str, Any]]:
        try:
            if self.path.stat().st_size > self.MAX_RECORD_BYTES * self.MAX_RECORDS:
                return []
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(value, list):
            return []
        records = [self._normalize_record(item) for item in value[: self.MAX_RECORDS]]
        return [record for record in records if record is not None]

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False
