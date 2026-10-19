from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.control_plane.domain.models import (
    BackupTargetRecord,
    CacheGenerationRecord,
    IndexStatusRecord,
    InventorySnapshot,
    JobRecord,
    JobStatus,
    NotificationChannelRecord,
    RetentionPolicyRecord,
    SecretRecord,
    SettingsRecord,
    SnapshotReadRequest,
    SnapshotReadResponse,
    SnapshotRecord,
    StorageProfileRecord,
    TargetStatsRecord,
    WorkerEnrollmentRecord,
    WorkerRecord,
)


class WorkerRepository(ABC):
    @abstractmethod
    def save(self, worker: WorkerRecord) -> WorkerRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, worker_id: str) -> Optional[WorkerRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[WorkerRecord]:
        raise NotImplementedError

    @abstractmethod
    def find_by_name(self, name: str) -> Optional[WorkerRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, worker_id: str) -> bool:
        raise NotImplementedError


class WorkerEnrollmentRepository(ABC):
    @abstractmethod
    def save(self, enrollment: WorkerEnrollmentRecord) -> WorkerEnrollmentRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, enrollment_id: str) -> Optional[WorkerEnrollmentRecord]:
        raise NotImplementedError

    @abstractmethod
    def get_by_token_hash(self, token_hash: str) -> Optional[WorkerEnrollmentRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[WorkerEnrollmentRecord]:
        raise NotImplementedError


class InventoryRepository(ABC):
    @abstractmethod
    def save(self, snapshot: InventorySnapshot) -> InventorySnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_by_worker(self, worker_id: str) -> Optional[InventorySnapshot]:
        raise NotImplementedError

    @abstractmethod
    def delete_by_worker(self, worker_id: str) -> bool:
        raise NotImplementedError


class TargetRepository(ABC):
    @abstractmethod
    def save(self, target: BackupTargetRecord) -> BackupTargetRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, target_id: str) -> Optional[BackupTargetRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[BackupTargetRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, target_id: str) -> bool:
        raise NotImplementedError


JOB_LISTING_SORT_KEYS = ("submitted_at", "started_at", "finished_at", "status", "command", "trigger", "target_id", "id")
JOB_LISTING_FILTER_KEYS = ("status", "command", "trigger", "target_id", "id_prefix", "started_from", "finished_to")
JOB_STATS_DAY_WINDOW = 7


def _job_matches_listing_filters(job: JobRecord, filters: Dict[str, Any]) -> bool:
    status = filters.get("status")
    if status and JobStatus.normalize(job.status) != status:
        return False
    for key in ("command", "trigger", "target_id"):
        value = filters.get(key)
        if value and getattr(job, key) != value:
            return False
    id_prefix = filters.get("id_prefix")
    if id_prefix and not str(job.id).lower().startswith(str(id_prefix).lower()):
        return False
    started_from = filters.get("started_from")
    if started_from and (job.started_at is None or job.started_at < started_from):
        return False
    finished_to = filters.get("finished_to")
    if finished_to and (job.finished_at is None or job.finished_at > finished_to):
        return False
    return True


def apply_job_listing_query(
    jobs: List[JobRecord],
    filters: Optional[Dict[str, Any]] = None,
    sort: str = "submitted_at",
    order: str = "desc",
) -> List[JobRecord]:
    """Filter and sort jobs with the same semantics as the SQL listing.

    Sort keys are whitelisted, ``id DESC`` is always the tiebreak and NULL values
    sort first ascending / last descending exactly like SQLite does.
    """
    if sort not in JOB_LISTING_SORT_KEYS:
        raise ValueError(f"unsupported sort key: {sort}")
    if order not in ("asc", "desc"):
        raise ValueError(f"unsupported sort order: {order}")
    active = {key: value for key, value in (filters or {}).items() if value}
    selected = [job for job in jobs if _job_matches_listing_filters(job, active)]
    selected.sort(key=lambda item: item.id, reverse=True)

    def sort_value(item: JobRecord):
        value = JobStatus.normalize(item.status) if sort == "status" else getattr(item, sort)
        return (value is not None, value)

    selected.sort(key=sort_value, reverse=(order == "desc"))
    return selected


def paginate_job_listing(jobs: List[JobRecord], limit: Optional[int], offset: int) -> List[JobRecord]:
    start = max(0, offset)
    if limit is not None and limit > 0:
        return jobs[start : start + limit]
    return jobs[start:] if start > 0 else jobs


def job_stats_day_keys(now: datetime, tz_offset_minutes: int, days: int = JOB_STATS_DAY_WINDOW) -> List[str]:
    today = (now + timedelta(minutes=tz_offset_minutes)).date()
    return [(today - timedelta(days=index)).isoformat() for index in range(days - 1, -1, -1)]


def build_job_stats(
    total: int,
    status_counts: Dict[str, int],
    day_status_counts: Dict[Tuple[str, str], int],
    day_keys: List[str],
) -> Dict[str, Any]:
    by_day = []
    for day in day_keys:
        counts = {status: count for (row_day, status), count in day_status_counts.items() if row_day == day}
        by_day.append(
            {
                "date": day,
                "total": sum(counts.values()),
                "success": counts.get(JobStatus.SUCCEEDED, 0),
                "failed": counts.get(JobStatus.FAILED, 0),
            }
        )
    return {"total": total, "by_status": dict(sorted(status_counts.items())), "by_day": by_day}


def compute_job_stats(jobs: List[JobRecord], tz_offset_minutes: int = 0, now: Optional[datetime] = None) -> Dict[str, Any]:
    reference = now or datetime.utcnow()
    day_keys = job_stats_day_keys(reference, tz_offset_minutes)
    shift = timedelta(minutes=tz_offset_minutes)
    status_counts: Dict[str, int] = {}
    day_status_counts: Dict[Tuple[str, str], int] = {}
    for job in jobs:
        status = JobStatus.normalize(job.status)
        status_counts[status] = status_counts.get(status, 0) + 1
        if job.started_at is None:
            continue
        key = ((job.started_at + shift).date().isoformat(), status)
        day_status_counts[key] = day_status_counts.get(key, 0) + 1
    return build_job_stats(len(jobs), status_counts, day_status_counts, day_keys)


class JobRepository(ABC):
    @abstractmethod
    def save(self, job: JobRecord) -> JobRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, job_id: str) -> Optional[JobRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[JobRecord]:
        raise NotImplementedError

    def list_for_listing(
        self,
        limit: Optional[int] = None,
        offset: int = 0,
        filters: Optional[Dict[str, Any]] = None,
        sort: str = "submitted_at",
        order: str = "desc",
    ) -> Tuple[List[JobRecord], int]:
        jobs = apply_job_listing_query(self.list(), filters, sort, order)
        return paginate_job_listing(jobs, limit, offset), len(jobs)

    def list_started_between(self, command: str, start: datetime, end: datetime) -> List[JobRecord]:
        start_utc = start.astimezone(timezone.utc).replace(tzinfo=None) if start.tzinfo else start
        end_utc = end.astimezone(timezone.utc).replace(tzinfo=None) if end.tzinfo else end
        if end_utc <= start_utc:
            return []
        # The audit report aggregates per-job result_summary metrics (T5 S2), so
        # the date-bounded report query returns full records (with result_summary)
        # rather than the lightweight listing projection. list_for_listing stays
        # lightweight for UI/listing callers; apply_job_listing_query preserves the
        # same command filter and started_at-asc ordering on the full record set.
        jobs = apply_job_listing_query(self.list(), filters={"command": command}, sort="started_at", order="asc")
        selected = []
        for job in jobs:
            started_at = job.started_at
            if started_at is None:
                continue
            if started_at.tzinfo:
                started_at = started_at.astimezone(timezone.utc).replace(tzinfo=None)
            if start_utc <= started_at < end_utc:
                selected.append(job)
        return selected

    def job_stats(self, tz_offset_minutes: int = 0, now: Optional[datetime] = None) -> Dict[str, Any]:
        return compute_job_stats(self.list(), tz_offset_minutes, now)

    @abstractmethod
    def claim_pending_for_worker(
        self,
        worker_id: str,
        lease_duration_seconds: int = 300,
        priority_commands: Optional[Tuple[str, ...]] = None,
    ) -> List[JobRecord]:
        """Claim one oldest pending job, preferring listed commands, and start its lease."""
        raise NotImplementedError

    @abstractmethod
    def reconcile_expired_leases(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def renew_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        lease_duration_seconds: int = 300,
    ) -> Optional[JobRecord]:
        raise NotImplementedError

    @abstractmethod
    def update_progress(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        sequence: int,
        progress: Dict[str, Any],
        log_lines: List[str],
        result_summary: Optional[Dict[str, Any]] = None,
    ) -> Optional[JobRecord]:
        raise NotImplementedError


class StorageProfileRepository(ABC):
    @abstractmethod
    def save(self, profile: StorageProfileRecord) -> StorageProfileRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, profile_id: str) -> Optional[StorageProfileRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[StorageProfileRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, profile_id: str) -> bool:
        raise NotImplementedError


class SecretRepository(ABC):
    @abstractmethod
    def save(self, secret: SecretRecord) -> SecretRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, secret_id: str) -> Optional[SecretRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[SecretRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, secret_id: str) -> None:
        raise NotImplementedError


class NotificationChannelRepository(ABC):
    @abstractmethod
    def save(self, channel: NotificationChannelRecord) -> NotificationChannelRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, channel_id: str) -> Optional[NotificationChannelRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[NotificationChannelRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete(self, channel_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def claim_delivery_attempt(self, job_id: str, channel_id: str) -> bool:
        """Return true only when this job/channel pair is claimed for delivery."""
        raise NotImplementedError


class SnapshotRepository(ABC):
    @abstractmethod
    def replace_for_target(self, target_id: str, snapshots: List[SnapshotRecord]) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_by_target(self, target_id: str) -> List[SnapshotRecord]:
        raise NotImplementedError


class RetentionPolicyRepository(ABC):
    @abstractmethod
    def save(self, policy: RetentionPolicyRecord) -> RetentionPolicyRecord:
        raise NotImplementedError

    @abstractmethod
    def get(self, policy_id: str) -> Optional[RetentionPolicyRecord]:
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[RetentionPolicyRecord]:
        raise NotImplementedError


class TargetStatsRepository(ABC):
    @abstractmethod
    def save(self, stats_record: TargetStatsRecord) -> TargetStatsRecord:
        raise NotImplementedError

    @abstractmethod
    def get_by_target(self, target_id: str) -> Optional[TargetStatsRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete_by_worker(self, worker_id: str) -> bool:
        raise NotImplementedError


class SettingsRepository(ABC):
    @abstractmethod
    def get(self) -> Optional[SettingsRecord]:
        raise NotImplementedError

    @abstractmethod
    def save(self, settings: SettingsRecord) -> SettingsRecord:
        raise NotImplementedError


class CacheRepository(ABC):
    """Persistent per-target restic cache metadata (generation/invalidation)."""

    @abstractmethod
    def get_generation(self, target_id: str, repository_fingerprint: str) -> Optional[CacheGenerationRecord]:
        raise NotImplementedError

    @abstractmethod
    def bump_generation(self, target_id: str, repository_fingerprint: str) -> CacheGenerationRecord:
        raise NotImplementedError

    @abstractmethod
    def cleanup_orphaned(self, active_keys: List[Tuple[str, str]]) -> int:
        raise NotImplementedError


class IndexRepository(ABC):
    """Bounded eager-index status metadata per snapshot."""

    @abstractmethod
    def upsert_status(self, record: IndexStatusRecord) -> IndexStatusRecord:
        raise NotImplementedError

    @abstractmethod
    def get_status(self, target_id: str, snapshot_id: str) -> Optional[IndexStatusRecord]:
        raise NotImplementedError

    @abstractmethod
    def list_by_target(self, target_id: str) -> List[IndexStatusRecord]:
        raise NotImplementedError

    @abstractmethod
    def delete_for_target(self, target_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def cleanup_orphaned(self, active_target_ids: List[str]) -> int:
        raise NotImplementedError


class InteractiveJobPort(ABC):
    """Bounded interactive lane beside durable jobs for explorer reads."""

    @abstractmethod
    def submit(self, request: SnapshotReadRequest) -> str:
        raise NotImplementedError

    @abstractmethod
    def get_result(self, job_id: str) -> Optional[SnapshotReadResponse]:
        raise NotImplementedError

    @abstractmethod
    def cancel(self, job_id: str) -> bool:
        raise NotImplementedError
