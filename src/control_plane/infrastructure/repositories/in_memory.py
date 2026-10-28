import hmac
import secrets
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from src.control_plane.application.ports.ports import (
    CacheRepository,
    DashboardReadSnapshotRepository,
    IndexRepository,
    InventoryRepository,
    JobRepository,
    NotificationChannelRepository,
    RetentionPolicyRepository,
    SecretRepository,
    SettingsRepository,
    SnapshotRepository,
    StorageProfileRepository,
    TargetStatsRepository,
    TargetRepository,
    WorkerEnrollmentRepository,
    WorkerRepository,
    apply_job_listing_query,
    compute_job_stats,
    paginate_job_listing,
)
from src.control_plane.domain.models import (
    BackupTargetRecord,
    CacheGenerationRecord,
    DashboardReadSnapshotRecord,
    IndexStatusRecord,
    InventorySnapshot,
    JobRecord,
    JobStatus,
    NotificationChannelRecord,
    RetentionPolicyRecord,
    SecretRecord,
    SettingsRecord,
    SnapshotRecord,
    StorageProfileRecord,
    TargetStatsRecord,
    WorkerEnrollmentRecord,
    WorkerRecord,
    utcnow,
)


LEASE_DIAGNOSTIC_CODE = "worker_lease_expired"
LEASE_DIAGNOSTIC_CATEGORY = "worker_interrupted"
LEASE_DIAGNOSTIC_MESSAGE = (
    "Control Plane observed that the worker lease expired before a terminal result arrived; "
    "the worker or runtime cause is unknown."
)


def _datetime_text(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        text = value.isoformat()
        return f"{text}Z" if value.utcoffset() is None else text
    return value.isoformat() if hasattr(value, "isoformat") else value if isinstance(value, str) else None


def _lease_context_for_job(job: JobRecord, now=None) -> Dict[str, Any]:
    summary = job.result_summary if isinstance(job.result_summary, dict) else {}
    current = summary.get("lease_context")
    context = dict(current) if isinstance(current, dict) else {}
    context.setdefault("job_id", job.id)
    context.setdefault("target_id", job.target_id)
    context.setdefault("worker_id", job.owner_worker_id or job.worker_id)
    context.setdefault("command", job.command)
    if job.lease_issued_at is not None:
        context.setdefault("lease_issued_at", _datetime_text(job.lease_issued_at))
    if job.lease_expires_at is not None:
        context.setdefault("lease_expires_at", _datetime_text(job.lease_expires_at))
    context.setdefault("last_repository_update_at", _datetime_text(job.updated_at))
    if now is not None:
        context.setdefault("observed_at", _datetime_text(now))
    return context


def _lease_diagnostics(job: JobRecord, now) -> Dict[str, Any]:
    context = _lease_context_for_job(job, now)
    summary = job.result_summary if isinstance(job.result_summary, dict) else {}
    try:
        sequence = int(context.get("last_progress_sequence", summary.get("progress_sequence", 0)) or 0)
    except (TypeError, ValueError):
        sequence = 0
    observed = {
        "job_id": context.get("job_id") or job.id,
        "target_id": context.get("target_id", job.target_id),
        "worker_id": context.get("worker_id") or job.owner_worker_id or job.worker_id,
        "command": context.get("command") or job.command,
        "lease_issued_at": context.get("lease_issued_at"),
        "lease_expires_at": context.get("lease_expires_at"),
        "last_repository_update_at": context.get("last_repository_update_at") or _datetime_text(job.updated_at),
        "last_progress_at": context.get("last_progress_at"),
        "last_progress_sequence": sequence,
        "worker_status": context.get("worker_status"),
        "worker_last_seen_at": context.get("worker_last_seen_at"),
    }
    renewal = context.get("last_renewal")
    if isinstance(renewal, dict):
        observed["last_renewal"] = dict(renewal)
    return {
        "code": LEASE_DIAGNOSTIC_CODE,
        "category": LEASE_DIAGNOSTIC_CATEGORY,
        "message": LEASE_DIAGNOSTIC_MESSAGE,
        "observed": observed,
        "unknown": ["terminal_result", "root_cause"],
    }


class InMemoryWorkerRepository(WorkerRepository):
    def __init__(self):
        self._items: Dict[str, WorkerRecord] = {}
        self._lock = Lock()

    def save(self, worker: WorkerRecord) -> WorkerRecord:
        with self._lock:
            self._items[worker.id] = worker
        return worker

    def get(self, worker_id: str) -> Optional[WorkerRecord]:
        return self._items.get(worker_id)

    def find_by_name(self, name: str) -> Optional[WorkerRecord]:
        for worker in sorted(self._items.values(), key=lambda item: item.created_at):
            if worker.name == name:
                return worker
        return None

    def list(self) -> List[WorkerRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)

    def delete(self, worker_id: str) -> bool:
        with self._lock:
            return self._items.pop(worker_id, None) is not None


class InMemoryWorkerEnrollmentRepository(WorkerEnrollmentRepository):
    def __init__(self):
        self._items: Dict[str, WorkerEnrollmentRecord] = {}
        self._lock = Lock()

    def save(self, enrollment: WorkerEnrollmentRecord) -> WorkerEnrollmentRecord:
        with self._lock:
            self._items[enrollment.id] = enrollment
        return enrollment

    def get(self, enrollment_id: str) -> Optional[WorkerEnrollmentRecord]:
        return self._items.get(enrollment_id)

    def get_by_token_hash(self, token_hash: str) -> Optional[WorkerEnrollmentRecord]:
        for enrollment in self._items.values():
            if enrollment.token_hash == token_hash:
                return enrollment
        return None

    def list(self) -> List[WorkerEnrollmentRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)


class InMemoryInventoryRepository(InventoryRepository):
    def __init__(self):
        self._items: Dict[str, InventorySnapshot] = {}
        self._lock = Lock()

    def save(self, snapshot: InventorySnapshot) -> InventorySnapshot:
        with self._lock:
            self._items[snapshot.worker_id] = snapshot
        return snapshot

    def get_by_worker(self, worker_id: str) -> Optional[InventorySnapshot]:
        return self._items.get(worker_id)

    def delete_by_worker(self, worker_id: str) -> bool:
        with self._lock:
            return self._items.pop(worker_id, None) is not None


class InMemoryTargetRepository(TargetRepository):
    def __init__(self):
        self._items: Dict[str, BackupTargetRecord] = {}
        self._lock = Lock()

    def save(self, target: BackupTargetRecord) -> BackupTargetRecord:
        with self._lock:
            self._items[target.id] = target
        return target

    def get(self, target_id: str) -> Optional[BackupTargetRecord]:
        return self._items.get(target_id)

    def list(self) -> List[BackupTargetRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)

    def delete(self, target_id: str) -> bool:
        with self._lock:
            existed = target_id in self._items
            self._items.pop(target_id, None)
            return existed


class InMemoryJobRepository(JobRepository):
    MAX_LOG_LINES = 1000
    MAX_LOG_CHARS = 512 * 1024

    def __init__(self):
        self._items: Dict[str, JobRecord] = {}
        self._lock = Lock()

    def save(self, job: JobRecord) -> JobRecord:
        with self._lock:
            job.status = JobStatus.normalize(job.status)
            self._items[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[JobRecord]:
        return self._items.get(job_id)

    def list(self) -> List[JobRecord]:
        return sorted(self._items.values(), key=lambda item: item.submitted_at, reverse=True)

    def list_for_lease_context(self) -> List[JobRecord]:
        with self._lock:
            return [
                job
                for job in self._items.values()
                if JobStatus.normalize(job.status) == JobStatus.IN_PROGRESS and job.lease_expires_at is not None
            ]

    def update_lease_context(self, job_id: str, lease_context: Dict[str, Any]) -> bool:
        with self._lock:
            job = self._items.get(job_id)
            if job is None:
                return False
            summary = dict(job.result_summary) if isinstance(job.result_summary, dict) else {}
            summary["lease_context"] = dict(lease_context)
            job.result_summary = summary
            return True

    def list_for_listing(
        self,
        limit: Optional[int] = None,
        offset: int = 0,
        filters: Optional[Dict[str, Any]] = None,
        sort: str = "submitted_at",
        order: str = "desc",
    ) -> Tuple[List[JobRecord], int]:
        with self._lock:
            jobs = apply_job_listing_query(list(self._items.values()), filters, sort, order)
        total = len(jobs)
        jobs = paginate_job_listing(jobs, limit, offset)
        return [
            JobRecord(
                id=job.id,
                worker_id=job.worker_id,
                command=job.command,
                requested_by=job.requested_by,
                target_id=job.target_id,
                trigger=job.trigger,
                status=JobStatus.normalize(job.status),
                attempt_count=job.attempt_count,
                submitted_at=job.submitted_at,
                started_at=job.started_at,
                finished_at=job.finished_at,
                updated_at=job.updated_at,
            )
            for job in jobs
        ], total

    def job_stats(self, tz_offset_minutes: int = 0, now: Optional[datetime] = None) -> Dict[str, Any]:
        with self._lock:
            jobs = list(self._items.values())
        return compute_job_stats(jobs, tz_offset_minutes, now)

    def _reconcile_expired_leases_locked(self, now) -> int:
        interruption_log = "Worker lease expired before terminal status was reported."
        reconciled = 0
        for job in self._items.values():
            lease_expires_at = job.lease_expires_at
            if lease_expires_at and lease_expires_at.tzinfo:
                lease_expires_at = lease_expires_at.astimezone(timezone.utc).replace(tzinfo=None)
            if (
                job.status == JobStatus.IN_PROGRESS
                and lease_expires_at
                and lease_expires_at <= now
            ):
                result_summary = dict(job.result_summary or {})
                result_summary["recovery"] = "awaiting_worker_confirmation"
                job.result_summary = result_summary
                result_summary["diagnostic_code"] = LEASE_DIAGNOSTIC_CODE
                result_summary["diagnostic_category"] = LEASE_DIAGNOSTIC_CATEGORY
                result_summary["diagnostic_message"] = LEASE_DIAGNOSTIC_MESSAGE
                result_summary["lease_diagnostics"] = _lease_diagnostics(job, now)
                result_summary.pop("lease_context", None)
                job.status = JobStatus.IN_PROGRESS
                # Keep owner_worker_id and lease_token: a late terminal report
                # from the owning worker must still prove ownership.
                job.lease_issued_at = None
                job.lease_expires_at = None
                job.result_summary = result_summary
                job.log_lines = list(job.log_lines or [])
                current_timestamps = (
                    list(job.log_timestamps)
                    if isinstance(job.log_timestamps, list) and len(job.log_timestamps) == len(job.log_lines)
                    else [None] * len(job.log_lines)
                )
                if interruption_log not in job.log_lines:
                    job.log_lines.append(interruption_log)
                    current_timestamps.append(None)
                job.log_timestamps = current_timestamps if any(current_timestamps) else None
                job.finished_at = None
                job.updated_at = now
                reconciled += 1
        return reconciled

    def reconcile_expired_leases(self) -> int:
        with self._lock:
            return self._reconcile_expired_leases_locked(utcnow())

    def renew_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        lease_duration_seconds: int = 300,
    ) -> Optional[JobRecord]:
        if lease_duration_seconds <= 0:
            raise ValueError("lease duration must be positive")
        with self._lock:
            now = utcnow()
            job = self._items.get(job_id)
            lease_expires_at = job.lease_expires_at if job else None
            if lease_expires_at and lease_expires_at.tzinfo:
                lease_expires_at = lease_expires_at.astimezone(timezone.utc).replace(tzinfo=None)
            if (
                job is None
                or job.status != JobStatus.IN_PROGRESS
                or job.owner_worker_id != worker_id
                or not hmac.compare_digest(job.lease_token or "", lease_token or "")
            ):
                return None
            job.lease_expires_at = now + timedelta(seconds=lease_duration_seconds)
            summary = dict(job.result_summary) if isinstance(job.result_summary, dict) else {}
            if summary.get("diagnostic_code") == LEASE_DIAGNOSTIC_CODE:
                for key in ("diagnostic_code", "diagnostic_category", "diagnostic_message", "lease_diagnostics", "recovery"):
                    summary.pop(key, None)
            context = _lease_context_for_job(job, now)
            context["lease_expires_at"] = _datetime_text(job.lease_expires_at)
            context["last_repository_update_at"] = _datetime_text(now)
            summary["lease_context"] = context
            job.result_summary = summary
            job.updated_at = now
            return job

    def update_progress(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        sequence: int,
        progress: Dict[str, Any],
        log_lines: List[str],
        result_summary: Optional[Dict[str, Any]] = None,
        log_timestamps: Optional[List[Optional[str]]] = None,
    ) -> Optional[JobRecord]:
        with self._lock:
            now = utcnow()
            job = self._items.get(job_id)
            lease_expires_at = job.lease_expires_at if job else None
            if lease_expires_at and lease_expires_at.tzinfo:
                lease_expires_at = lease_expires_at.astimezone(timezone.utc).replace(tzinfo=None)
            if (
                job is None
                or job.status != JobStatus.IN_PROGRESS
                or job.owner_worker_id != worker_id
                or not hmac.compare_digest(job.lease_token or "", lease_token or "")
            ):
                return None

            current_summary = dict(job.result_summary or {})
            try:
                current_sequence = int(current_summary.get("progress_sequence", 0) or 0)
            except (TypeError, ValueError):
                current_sequence = 0
            if sequence <= current_sequence:
                return job

            if isinstance(result_summary, dict):
                current_summary.update(result_summary)
            current_summary["progress"] = dict(progress)
            current_summary["progress_sequence"] = sequence
            context = _lease_context_for_job(job, now)
            context["last_repository_update_at"] = _datetime_text(now)
            context["last_progress_at"] = _datetime_text(now)
            context["last_progress_sequence"] = sequence
            current_summary["lease_context"] = context
            job.result_summary = current_summary
            current_lines = list(job.log_lines or [])
            current_timestamps = (
                list(job.log_timestamps)
                if isinstance(job.log_timestamps, list) and len(job.log_timestamps) == len(current_lines)
                else [None] * len(current_lines)
            )
            incoming_lines = list(log_lines or [])
            incoming_timestamps = (
                list(log_timestamps)
                if isinstance(log_timestamps, list) and len(log_timestamps) == len(incoming_lines)
                else [None] * len(incoming_lines)
            )
            combined_lines = current_lines + incoming_lines
            combined_timestamps = current_timestamps + incoming_timestamps
            offset = max(0, len(combined_lines) - self.MAX_LOG_LINES)
            job.log_lines = combined_lines[offset:]
            combined_timestamps = combined_timestamps[offset:]
            while job.log_lines and sum(len(line) for line in job.log_lines if isinstance(line, str)) > self.MAX_LOG_CHARS:
                job.log_lines.pop(0)
                combined_timestamps.pop(0)
            job.log_timestamps = combined_timestamps if any(combined_timestamps) else None
            job.updated_at = now
            return job

    def claim_pending_for_worker(
        self,
        worker_id: str,
        lease_duration_seconds: int = 300,
        priority_commands: Optional[Tuple[str, ...]] = None,
    ) -> List[JobRecord]:
        if lease_duration_seconds <= 0:
            raise ValueError("lease duration must be positive")
        with self._lock:
            now = utcnow()
            self._reconcile_expired_leases_locked(now)
            expires_at = now + timedelta(seconds=lease_duration_seconds)
            claimed = []
            preferred_commands = set(priority_commands or ())
            for job in sorted(
                self._items.values(),
                key=lambda item: (item.command not in preferred_commands, item.submitted_at),
            ):
                if job.worker_id != worker_id or job.status != JobStatus.PENDING:
                    continue
                job.status = JobStatus.IN_PROGRESS
                job.owner_worker_id = worker_id
                job.lease_token = secrets.token_urlsafe(32)
                job.lease_issued_at = now
                job.lease_expires_at = expires_at
                job.attempt_count = (job.attempt_count or 0) + 1
                job.started_at = now
                job.updated_at = now
                summary = dict(job.result_summary) if isinstance(job.result_summary, dict) else {}
                context = _lease_context_for_job(job, now)
                context.update(
                    {
                        "lease_issued_at": _datetime_text(now),
                        "lease_expires_at": _datetime_text(expires_at),
                        "last_repository_update_at": _datetime_text(now),
                    }
                )
                summary["lease_context"] = context
                job.result_summary = summary
                claimed.append(job)
                break
            return claimed


class InMemoryStorageProfileRepository(StorageProfileRepository):
    def __init__(self):
        self._items: Dict[str, StorageProfileRecord] = {}
        self._lock = Lock()

    def save(self, profile: StorageProfileRecord) -> StorageProfileRecord:
        with self._lock:
            self._items[profile.id] = profile
        return profile

    def get(self, profile_id: str) -> Optional[StorageProfileRecord]:
        return self._items.get(profile_id)

    def list(self) -> List[StorageProfileRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)

    def delete(self, profile_id: str) -> bool:
        with self._lock:
            return self._items.pop(profile_id, None) is not None


class InMemorySecretRepository(SecretRepository):
    def __init__(self):
        self._items: Dict[str, SecretRecord] = {}
        self._lock = Lock()

    def save(self, secret: SecretRecord) -> SecretRecord:
        with self._lock:
            self._items[secret.id] = secret
        return secret

    def get(self, secret_id: str) -> Optional[SecretRecord]:
        return self._items.get(secret_id)

    def list(self) -> List[SecretRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)

    def delete(self, secret_id: str) -> None:
        with self._lock:
            self._items.pop(secret_id, None)


class InMemoryNotificationChannelRepository(NotificationChannelRepository):
    def __init__(self):
        self._items: Dict[str, NotificationChannelRecord] = {}
        self._delivery_attempts = set()
        self._lock = Lock()

    def save(self, channel: NotificationChannelRecord) -> NotificationChannelRecord:
        with self._lock:
            self._items[channel.id] = channel
        return channel

    def get(self, channel_id: str) -> Optional[NotificationChannelRecord]:
        with self._lock:
            return self._items.get(channel_id)

    def list(self) -> List[NotificationChannelRecord]:
        with self._lock:
            return sorted(self._items.values(), key=lambda item: item.created_at)

    def delete(self, channel_id: str) -> bool:
        with self._lock:
            return self._items.pop(channel_id, None) is not None

    def claim_delivery_attempt(self, job_id: str, channel_id: str) -> bool:
        key = (job_id, channel_id)
        with self._lock:
            if key in self._delivery_attempts:
                return False
            self._delivery_attempts.add(key)
            return True


class InMemorySnapshotRepository(SnapshotRepository):
    def __init__(self):
        self._items: Dict[str, List[SnapshotRecord]] = {}
        self._lock = Lock()

    def replace_for_target(self, target_id: str, snapshots: List[SnapshotRecord]) -> None:
        with self._lock:
            self._items[target_id] = list(snapshots)

    def list_by_target(self, target_id: str) -> List[SnapshotRecord]:
        return list(self._items.get(target_id, []))


class InMemoryRetentionPolicyRepository(RetentionPolicyRepository):
    def __init__(self):
        self._items: Dict[str, RetentionPolicyRecord] = {}
        self._lock = Lock()

    def save(self, policy: RetentionPolicyRecord) -> RetentionPolicyRecord:
        with self._lock:
            self._items[policy.id] = policy
        return policy

    def get(self, policy_id: str) -> Optional[RetentionPolicyRecord]:
        return self._items.get(policy_id)

    def list(self) -> List[RetentionPolicyRecord]:
        return sorted(self._items.values(), key=lambda item: item.created_at)


class InMemoryTargetStatsRepository(TargetStatsRepository):
    def __init__(self):
        self._items: Dict[str, TargetStatsRecord] = {}
        self._lock = Lock()

    def save(self, stats_record: TargetStatsRecord) -> TargetStatsRecord:
        with self._lock:
            self._items[stats_record.target_id] = stats_record
        return stats_record

    def get_by_target(self, target_id: str) -> Optional[TargetStatsRecord]:
        return self._items.get(target_id)

    def delete_by_worker(self, worker_id: str) -> bool:
        with self._lock:
            stale_targets = [target_id for target_id, item in self._items.items() if item.worker_id == worker_id]
            for target_id in stale_targets:
                self._items.pop(target_id, None)
            return bool(stale_targets)


class InMemorySettingsRepository(SettingsRepository):
    def __init__(self):
        self._item: Optional[SettingsRecord] = None
        self._lock = Lock()

    def get(self) -> Optional[SettingsRecord]:
        with self._lock:
            return self._item

    def save(self, settings: SettingsRecord) -> SettingsRecord:
        with self._lock:
            self._item = settings
        return settings


class InMemoryDashboardReadSnapshotRepository(DashboardReadSnapshotRepository):
    """Memory-backed last-good dashboard read-model snapshots (same port as SQLite)."""

    def __init__(self):
        self._items: Dict[str, DashboardReadSnapshotRecord] = {}
        self._lock = Lock()

    def save(self, record: DashboardReadSnapshotRecord) -> None:
        with self._lock:
            self._items[record.storage_key()] = record

    def load_all(self) -> List[DashboardReadSnapshotRecord]:
        with self._lock:
            return [self._items[key] for key in sorted(self._items)]


class InMemoryCacheRepository(CacheRepository):
    def __init__(self):
        self._items: Dict[Tuple[str, str], CacheGenerationRecord] = {}
        self._lock = Lock()

    def get_generation(self, target_id: str, repository_fingerprint: str) -> Optional[CacheGenerationRecord]:
        return self._items.get((target_id, repository_fingerprint))

    def bump_generation(self, target_id: str, repository_fingerprint: str) -> CacheGenerationRecord:
        with self._lock:
            key = (target_id, repository_fingerprint)
            current = self._items.get(key)
            record = CacheGenerationRecord(
                target_id=target_id,
                repository_fingerprint=repository_fingerprint,
                generation=(current.generation if current else 0) + 1,
            )
            self._items[key] = record
            return record

    def cleanup_orphaned(self, active_keys: List[Tuple[str, str]]) -> int:
        with self._lock:
            active = set(active_keys)
            stale = [key for key in self._items if key not in active]
            for key in stale:
                self._items.pop(key, None)
            return len(stale)


class InMemoryIndexRepository(IndexRepository):
    def __init__(self):
        self._items: Dict[Tuple[str, str], IndexStatusRecord] = {}
        self._lock = Lock()

    def upsert_status(self, record: IndexStatusRecord) -> IndexStatusRecord:
        with self._lock:
            self._items[(record.target_id, record.snapshot_id)] = record
        return record

    def get_status(self, target_id: str, snapshot_id: str) -> Optional[IndexStatusRecord]:
        return self._items.get((target_id, snapshot_id))

    def list_by_target(self, target_id: str) -> List[IndexStatusRecord]:
        return [
            record
            for (record_target_id, _), record in sorted(self._items.items(), key=lambda item: item[0][1])
            if record_target_id == target_id
        ]

    def delete_for_target(self, target_id: str) -> None:
        with self._lock:
            stale = [key for key in self._items if key[0] == target_id]
            for key in stale:
                self._items.pop(key, None)

    def cleanup_orphaned(self, active_target_ids: List[str]) -> int:
        with self._lock:
            active = set(active_target_ids)
            stale = [key for key in self._items if key[0] not in active]
            for key in stale:
                self._items.pop(key, None)
            return len(stale)
