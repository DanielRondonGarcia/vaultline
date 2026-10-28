from typing import Any, Dict, List, Optional

from src.worker_agent.infrastructure.security.terminal_report_journal import WorkerTerminalReportJournal


class WorkerProgressReportJournal(WorkerTerminalReportJournal):
    """Private atomic spool of unacknowledged, sequenced progress batches."""

    MAX_RECORDS = 64
    MAX_RECORD_BYTES = 64 * 1024
    MAX_AGE_SECONDS = WorkerTerminalReportJournal.MAX_AGE_SECONDS

    @classmethod
    def batch(cls, worker_id, job_id, lease_token, sequence, progress, log_lines, log_timestamps=None):
        return cls.bounded_record(
            worker_id=worker_id, job_id=job_id, lease_token=lease_token,
            status="progress", result_summary={"sequence": sequence, "progress": progress},
            log_lines=log_lines, log_timestamps=log_timestamps,
        )

    @classmethod
    def _normalize_record(cls, value: Any) -> Optional[Dict[str, Any]]:
        record = super()._normalize_record(value)
        if record is None or record["status"] != "progress":
            return None
        summary = record["result_summary"]
        sequence = summary.get("sequence")
        if type(sequence) is not int or not 0 < sequence <= 1_000_000_000:
            return None
        if not isinstance(summary.get("progress"), dict):
            return None
        return record

    def write(self, records: List[Dict[str, Any]]) -> None:
        super().write(records)
        if self.path.parent.exists():
            self.path.parent.chmod(0o700)
