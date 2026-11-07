"""Worker-side ownership of the backup schedule.

The control plane normally dispatches scheduled backups. While it cannot reach
the worker (or the worker cannot reach it), the worker keeps its last resolved
schedule, evaluates the cron locally, executes the due ``backup.run`` through
the normal job execution path and stores the pending result in the durable
``OfflineResultOutbox``. On reconnection the results are replayed in order,
idempotently, and control-plane rejections and connectivity problems keep
them queued. Capacity losses stay observable.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.control_plane.application.services.scheduler_service import cron_matches
from src.control_plane.domain.models import JobStatus
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerJobExecutionResult
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneHTTPError

logger = logging.getLogger(__name__)

# Control planes without the schedule contract answer 404/405/501; the worker
# keeps the durable last configuration and pending results instead of failing.
LEGACY_CONTROL_PLANE_STATUS_CODES = frozenset({404, 405, 501})
LEGACY_SCHEDULER_TIMEZONE = "America/Bogota"
MAX_RECORD_LOG_LINES = 500
MAX_RECORD_LOG_LINE_CHARS = 4096
MAX_RECORD_RESULT_BYTES = 96 * 1024
MAX_RECORD_BYTES = 192 * 1024


def _resolve_zone(name: Optional[str]):
    configured = name or LEGACY_SCHEDULER_TIMEZONE
    try:
        return ZoneInfo(configured)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"Worker schedule timezone is unavailable: {configured}") from exc


def _is_legacy_response(error: Exception) -> bool:
    return isinstance(error, ControlPlaneHTTPError) and error.code in LEGACY_CONTROL_PLANE_STATUS_CODES


class LocalScheduleService:
    """Evaluate the durable local schedule and synchronize offline results."""

    def __init__(
        self,
        *,
        journal,
        outbox,
        fetch: Callable[[str], Optional[Dict[str, Any]]],
        deliver: Callable[[str, Dict[str, Any]], None],
        executor: Callable[..., WorkerJobExecutionResult],
        now: Optional[Callable[[], datetime]] = None,
        acknowledge: Optional[Callable[[str, str], Any]] = None,
    ):
        self._journal = journal
        self._outbox = outbox
        self._fetch = fetch
        self._deliver = deliver
        self._executor = executor
        self._acknowledge = acknowledge
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._classify_rejection = WorkerAgentService._is_definitive_control_plane_rejection
        self._last_dropped_total = 0

    # ------------------------------------------------------------------ API

    def poll(self, worker_id: Optional[str]) -> Dict[str, Any]:
        """Refresh, replay and evaluate once; never raises to the caller's loop."""
        poll: Dict[str, Any] = {
            "schedule": "not_registered", "replayed": 0, "executed": 0,
            "offline": {"executed": 0, "ids": []}, "dropped_results": 0,
        }
        if not isinstance(worker_id, str) or not worker_id:
            return poll
        # Capture the due minute before network calls, but do not run a stale
        # worker schedule against a reachable legacy CP that may own the cron.
        evaluation_time = self._now()
        poll["schedule"] = self.refresh(worker_id)
        if poll["schedule"] != "legacy_control_plane":
            offline = self.evaluate(worker_id, at=evaluation_time)
            poll["executed"] = offline["executed"]
            poll["offline"] = offline
        poll["replayed"] = self.replay(worker_id)
        dropped = self.outbox_dropped_count()
        poll["dropped_results"] = dropped
        if dropped > self._last_dropped_total:
            logger.warning(
                "Offline results were dropped by outbox capacity: %s record(s) lost in total",
                dropped,
            )
        self._last_dropped_total = dropped
        return poll

    def refresh(self, worker_id: str) -> str:
        """Download schedule state, keeping the durable last configuration (S2)."""
        try:
            state = self._fetch(worker_id)
            if state is None:
                return "legacy_control_plane"
        except Exception as exc:
            if _is_legacy_response(exc):
                return "legacy_control_plane"
            logger.warning(
                "Offline schedule refresh failed; keeping last configuration (error_type=%s)",
                exc.__class__.__name__,
            )
            return "kept_last_configuration"
        try:
            previous = self._journal.load()
            revision = state.get("revision")
            if isinstance(previous, dict) and previous.get("revision") == revision and revision:
                status = "unchanged"
            else:
                self._journal.save(state)
                status = "updated"
            if self._acknowledge is not None:
                try:
                    self._acknowledge(worker_id, revision)
                except Exception as exc:
                    logger.warning("Schedule acknowledgement failed (error_type=%s)", exc.__class__.__name__)
            return status
        except ValueError as exc:
            logger.warning(
                "Incompatible control-plane schedule state ignored; keeping last configuration (error_type=%s)",
                exc.__class__.__name__,
            )
            return "kept_last_configuration"

    def evaluate(self, worker_id: str, *, at: Optional[datetime] = None) -> Dict[str, Any]:
        """Run the due scheduled backups offline through the normal job path."""
        state = self._journal.load()
        entries = state.get("entries", []) if isinstance(state, dict) else []
        executed: List[str] = []
        evaluation_time = at or self._now()
        for entry in entries:
            if entry.get("enabled") is False:
                continue
            target_id = entry.get("target_id")
            if not isinstance(target_id, str) or not target_id:
                continue
            zone = _resolve_zone(entry.get("timezone") or (state or {}).get("timezone"))
            local_now = evaluation_time.astimezone(zone)
            if not cron_matches(entry.get("cron_expression"), local_now):
                continue
            slot_key = local_now.replace(second=0, microsecond=0).isoformat()
            completed = self._journal.completed_slots()
            if completed.get(target_id) == slot_key:
                continue
            # Mark before running: a crash between mark and execution skips at
            # most one backup, while run-then-mark could double-schedule it.
            self._journal.mark_slot(target_id, slot_key)
            record_id = f"offline:{target_id}:{slot_key}"
            self._execute_entry(worker_id, entry, record_id, slot_key, target_id)
            executed.append(record_id)
        return {"executed": len(executed), "ids": executed}

    def replay(self, worker_id: str) -> int:
        """Deliver pending offline results in order; never raises."""
        delivered = 0
        for record in self._outbox.pending():
            if not isinstance(record, dict):
                continue
            record_id = record.get("id")
            if record.get("worker_id") != worker_id:
                logger.warning("Offline result %s retained: worker identity changed", record_id)
                break
            try:
                response = self._deliver(worker_id, record)
                if (not isinstance(response, dict) or response.get("acknowledged") is not True
                        or response.get("job_id") != record_id):
                    raise ValueError("control plane did not acknowledge the offline result")
            except Exception as exc:
                if _is_legacy_response(exc):
                    logger.debug("Legacy control plane: offline result %s stays pending", record_id)
                    break
                if self._classify_rejection(exc):
                    logger.warning(
                        "Offline result %s rejected by the control plane (error_type=%s); retained",
                        record_id,
                        exc.__class__.__name__,
                    )
                    continue
                logger.warning(
                    "Offline result %s not delivered yet (error_type=%s); it is retried in order",
                    record_id,
                    exc.__class__.__name__,
                )
                break
            self._outbox.acknowledge(record_id)
            delivered += 1
        dropped = self.outbox_dropped_count()
        if dropped > self._last_dropped_total:
            logger.warning("Offline results were dropped by outbox capacity: %s record(s) lost in total", dropped)
            self._last_dropped_total = dropped
        return delivered

    def outbox_dropped_count(self) -> int:
        try:
            return int(self._outbox.dropped_count())
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------- internals

    def _execute_entry(
        self, worker_id: str, entry: Dict[str, Any], record_id: str, slot_key: str, target_id: str,
    ) -> None:
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        try:
            execution = self._executor(payload, entry, slot_key)
        except Exception as exc:
            logger.warning(
                "Offline scheduled backup failed for target %s (error_type=%s)",
                target_id,
                exc.__class__.__name__,
            )
            safe_error = WorkerAgentService._safe_job_text(payload, str(exc))[: WorkerAgentService.MAX_RESULT_ERROR_CHARS]
            execution = WorkerJobExecutionResult(
                status=JobStatus.FAILED,
                result_summary={
                    "target_id": target_id,
                    "command": "backup.run",
                    "error": safe_error,
                },
                log_lines=[safe_error],
            )
        self._store_result(worker_id, entry, record_id, execution, target_id)

    def _store_result(
        self,
        worker_id: str,
        entry: Dict[str, Any],
        record_id: str,
        execution: WorkerJobExecutionResult,
        target_id: str,
    ) -> None:
        dropped_before = self.outbox_dropped_count()
        record = self._bounded_record(worker_id, entry, record_id, execution, target_id)
        try:
            self._outbox.append(record)
        except ValueError as exc:
            if "already exists" in str(exc):
                logger.info("Offline result %s is already stored; ignoring duplicate", record_id)
                return
            raise
        dropped_after = self.outbox_dropped_count()
        if dropped_after > dropped_before:
            logger.warning(
                "Offline result storage is full: %s record(s) were dropped (total lost=%s)",
                dropped_after - dropped_before,
                dropped_after,
            )

    @classmethod
    def _bounded_record(
        cls, worker_id: str, entry: Dict[str, Any], record_id: str, execution: WorkerJobExecutionResult, target_id: str,
    ) -> Dict[str, Any]:
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}

        def redact(value, depth=0):
            if depth > 32:
                return "[truncated]"
            if isinstance(value, str):
                return WorkerAgentService._safe_job_text(payload, value)
            if isinstance(value, dict):
                return {WorkerAgentService._safe_job_text(payload, str(key)): redact(item, depth + 1)
                        for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item, depth + 1) for item in value]
            return value

        log_lines = [
            WorkerAgentService._safe_job_text(payload, line)[:MAX_RECORD_LOG_LINE_CHARS]
            for line in (execution.log_lines or [])
            if isinstance(line, str)
        ][-MAX_RECORD_LOG_LINES:]
        result_summary = redact(execution.result_summary) if isinstance(execution.result_summary, dict) else {}
        try:
            encoded_summary = json.dumps(result_summary, separators=(",", ":"), ensure_ascii=True, default=str)
        except (TypeError, ValueError, RecursionError):
            result_summary = {"error": "offline result summary was not serializable", "command": "backup.run"}
        else:
            if len(encoded_summary.encode("utf-8")) > MAX_RECORD_RESULT_BYTES:
                result_summary = {
                    "error": "offline result summary exceeded the permitted size",
                    "command": "backup.run",
                    "target_id": target_id,
                }
        record = {
            "id": record_id,
            "worker_id": worker_id,
            "target_id": target_id,
            "trigger": "offline_schedule",
            "cron_expression": entry.get("cron_expression"),
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "status": execution.status,
            "result_summary": result_summary,
            "log_lines": log_lines,
        }
        return record
