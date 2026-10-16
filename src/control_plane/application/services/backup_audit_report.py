from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.control_plane.domain.models import JobStatus


_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_BACKUP_COMMAND = "backup.run"
_RETENTION_FIELDS = (
    "keep_last",
    "keep_hourly",
    "keep_daily",
    "keep_weekly",
    "keep_monthly",
    "keep_yearly",
    "prune",
)
# Per-job Restic summary byte metrics captured by the worker (T5 S2). The keys
# are the additive result_summary fields; the labels are the report-facing names.
_BACKUP_SIZE_METRIC_SOURCES = (
    ("backup_bytes_processed", "bytes_processed"),
    ("backup_bytes_added", "bytes_added"),
)
_BACKUP_SIZE_AVAILABILITY_BASIS = (
    "Restic JSON summary total_bytes_processed (source bytes processed) and "
    "data_added (backend-reported stored bytes) captured per backup.run job; "
    "each metric is unavailable independently when a job has no exact Restic "
    "summary or uses a non-Restic strategy."
)


@dataclass(frozen=True)
class AuditPeriod:
    start_date: str
    end_date: str
    timezone_name: str
    start_utc: datetime
    end_utc: datetime


def parse_audit_period(start_date: str, end_date: str, timezone_name: str) -> AuditPeriod:
    if not isinstance(start_date, str) or not _DATE_PATTERN.fullmatch(start_date):
        raise ValueError("start_date must use YYYY-MM-DD")
    if not isinstance(end_date, str) or not _DATE_PATTERN.fullmatch(end_date):
        raise ValueError("end_date must use YYYY-MM-DD")
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("start_date and end_date must be valid calendar dates") from exc
    if start > end:
        raise ValueError("start_date must be on or before end_date")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError("scheduler timezone is required")
    try:
        zone = ZoneInfo(timezone_name.strip())
        end_exclusive = end + timedelta(days=1)
    except (OverflowError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("scheduler timezone or date range is invalid") from exc

    start_local = datetime.combine(start, time.min, tzinfo=zone)
    end_local = datetime.combine(end_exclusive, time.min, tzinfo=zone)
    return AuditPeriod(
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        timezone_name=zone.key,
        start_utc=start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_utc=end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _percentage(count: int, denominator: int) -> float | None:
    return round(count * 100 / denominator, 2) if denominator else None


def _job_counts(jobs: Iterable[Any]) -> dict[str, Any]:
    statuses = Counter(JobStatus.normalize(str(getattr(job, "status", "unknown") or "unknown")) for job in jobs)
    succeeded = statuses.get(JobStatus.SUCCEEDED, 0)
    failed = statuses.get(JobStatus.FAILED, 0)
    completed = succeeded + failed
    other_statuses = {
        status: count
        for status, count in sorted(statuses.items())
        if status not in (JobStatus.SUCCEEDED, JobStatus.FAILED)
    }
    return {
        "total": sum(statuses.values()),
        "completed": completed,
        "succeeded": succeeded,
        "failed": failed,
        "other": sum(other_statuses.values()),
        "other_statuses": other_statuses,
        "succeeded_percent": _percentage(succeeded, completed),
        "failed_percent": _percentage(failed, completed),
    }


def _retention_view(policy: Any | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    return {
        "id": str(getattr(policy, "id", "")),
        "name": str(getattr(policy, "name", "")),
        **{field: getattr(policy, field, None) for field in _RETENTION_FIELDS},
    }


def _job_backup_size_metrics(job: Any) -> dict[str, int]:
    """Return the exact nonnegative integer size metrics stored on a job.

    Only ``backup_bytes_processed`` / ``backup_bytes_added`` keys that hold an
    exact nonnegative integer (not bool/float/string) are returned, so a missing
    or invalid metric stays unavailable independently of its sibling.
    """
    summary = getattr(job, "result_summary", None)
    if not isinstance(summary, dict):
        return {}
    metrics: dict[str, int] = {}
    for source_key, _label in _BACKUP_SIZE_METRIC_SOURCES:
        value = summary.get(source_key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            metrics[source_key] = value
    return metrics


def _aggregate_backup_size(jobs: list[Any]) -> dict[str, Any]:
    """Aggregate per-job Restic byte metrics with explicit per-metric coverage.

    Exact values are summed across in-period ``backup.run`` jobs that reported a
    metric; a metric is ``None`` (never zero) when no job reported it. Coverage
    reports ``reported_jobs`` and ``unavailable_jobs`` per metric so partial
    totals are explicit.
    """
    total_jobs = len(jobs)
    sums: dict[str, int] = {source_key: 0 for source_key, _ in _BACKUP_SIZE_METRIC_SOURCES}
    reported: dict[str, int] = {source_key: 0 for source_key, _ in _BACKUP_SIZE_METRIC_SOURCES}
    for job in jobs:
        metrics = _job_backup_size_metrics(job)
        for source_key, _label in _BACKUP_SIZE_METRIC_SOURCES:
            if source_key in metrics:
                sums[source_key] += metrics[source_key]
                reported[source_key] += 1
    coverage = {
        label: {
            "reported_jobs": reported[source_key],
            "unavailable_jobs": total_jobs - reported[source_key],
        }
        for source_key, label in _BACKUP_SIZE_METRIC_SOURCES
    }
    return {
        label: (sums[source_key] if reported[source_key] else None)
        for source_key, label in _BACKUP_SIZE_METRIC_SOURCES
    } | {"coverage": coverage}


def _target_view(target: Any, workers: dict[str, Any], policies: dict[str, Any], jobs: list[Any], period: AuditPeriod, global_cron_expression: str | None, storage_profiles: dict[str, Any]) -> dict[str, Any]:
    worker = workers.get(str(getattr(target, "worker_id", "")))
    target_cron = str(getattr(target, "cron_expression", None) or "").strip()
    global_cron = str(global_cron_expression or "").strip()
    cron_expression = target_cron or global_cron or None
    cron_source = "target" if target_cron else "global" if global_cron else "manual"
    policy_id = str(getattr(target, "retention_policy_id", None) or "")
    counts = _job_counts(jobs)
    storage_profile = storage_profiles.get(str(getattr(target, "storage_profile_id", None) or ""))
    backend_type = str(getattr(storage_profile, "backend_type", "") or "") if storage_profile else ""
    profile_name = str(getattr(storage_profile, "name", "") or "") if storage_profile else ""
    return {
        "target_id": str(getattr(target, "id", "")),
        "service": str(getattr(target, "name", "")),
        "server": {
            "id": str(getattr(worker, "id", "")) if worker else None,
            "name": str(getattr(worker, "name", "")) if worker else None,
            "host": str(getattr(worker, "host_name", "")) if worker else None,
        },
        "runtime_type": str(getattr(target, "runtime_type", "") or ""),
        "backup_strategy": str(getattr(target, "backup_strategy", "") or ""),
        "backup_mode": str(getattr(target, "backup_mode", "") or ""),
        "enabled": bool(getattr(target, "enabled", False)),
        "resources": {
            "compose_project": getattr(target, "compose_project", None),
            "volumes": list(getattr(target, "volume_targets", []) or []),
            "filesystem_paths": list(getattr(target, "filesystem_paths", []) or []),
            "pvc_names": list(getattr(target, "pvc_names", []) or []),
        },
        "schedule": {
            "cron_expression": cron_expression,
            "source": cron_source,
            "timezone": period.timezone_name,
        },
        "retention": _retention_view(policies.get(policy_id)),
        "job_counts": {key: counts[key] for key in ("total", "completed", "succeeded", "failed", "other", "other_statuses")},
        "succeeded_percent": counts["succeeded_percent"],
        "failed_percent": counts["failed_percent"],
        "backup_size": _aggregate_backup_size(jobs),
        "storage_location": {
            "backend_type": backend_type or None,
            "storage_profile_name": profile_name or None,
            "path_storage": getattr(target, "path_storage", None),
        },
    }


def build_backup_audit_report(
    *,
    period: AuditPeriod,
    targets: Iterable[Any],
    workers: Iterable[Any],
    retention_policies: Iterable[Any],
    jobs: Iterable[Any],
    storage_profiles: Iterable[Any] = (),
    global_cron_expression: str | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    backup_jobs = [job for job in jobs if getattr(job, "command", None) == _BACKUP_COMMAND]
    jobs_by_target: dict[str, list[Any]] = defaultdict(list)
    for job in backup_jobs:
        jobs_by_target[str(getattr(job, "target_id", None) or "")].append(job)

    target_list = list(targets)
    worker_map = {str(worker.id): worker for worker in workers}
    policy_map = {str(policy.id): policy for policy in retention_policies}
    storage_profile_map = {str(profile.id): profile for profile in storage_profiles}
    matrix = [
        _target_view(
            target,
            worker_map,
            policy_map,
            jobs_by_target.get(str(target.id), []),
            period,
            global_cron_expression,
            storage_profile_map,
        )
        for target in sorted(target_list, key=lambda item: (str(item.name).casefold(), str(item.id)))
    ]
    summary = _job_counts(backup_jobs)
    backup_size_summary = _aggregate_backup_size(backup_jobs)
    backup_size_metrics_availability: dict[str, Any] = {}
    for source_key, label in _BACKUP_SIZE_METRIC_SOURCES:
        coverage = backup_size_summary["coverage"][label]
        reported_jobs = coverage["reported_jobs"]
        unavailable_jobs = coverage["unavailable_jobs"]
        available = reported_jobs > 0
        metric = {
            "source_field": (
                "total_bytes_processed"
                if source_key == "backup_bytes_processed"
                else "data_added"
            ),
            "label": (
                "source bytes processed"
                if source_key == "backup_bytes_processed"
                else "backend stored bytes"
            ),
            "available": available,
            "complete": available and unavailable_jobs == 0,
            "reported_jobs": reported_jobs,
            "unavailable_jobs": unavailable_jobs,
        }
        backup_size_metrics_availability[label] = metric
    configured_ids = {str(target.id) for target in target_list}
    summary_view = {
        "target_count": len(target_list),
        "backup_job_count": summary["total"],
        "completed_job_count": summary["completed"],
        "succeeded_count": summary["succeeded"],
        "failed_count": summary["failed"],
        "other_status_counts": summary["other_statuses"],
        "unmapped_target_job_count": sum(
            1 for job in backup_jobs if str(getattr(job, "target_id", None) or "") not in configured_ids
        ),
        "succeeded_percent": summary["succeeded_percent"],
        "failed_percent": summary["failed_percent"],
        "backup_size": backup_size_summary,
    }
    current_time = generated_at or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    return {
        "report": "backup_audit",
        "generated_at": current_time.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "period": {
            "start_date": period.start_date,
            "end_date": period.end_date,
            "timezone": period.timezone_name,
        },
        "availability": {
            "backup_size": {
                "available": any(metric["available"] for metric in backup_size_metrics_availability.values()),
                "complete": bool(backup_size_metrics_availability)
                and all(metric["complete"] for metric in backup_size_metrics_availability.values()),
                "basis": _BACKUP_SIZE_AVAILABILITY_BASIS,
                "metrics": backup_size_metrics_availability,
            },
            "storage_location": {
                "available": True,
                "basis": "target.path_storage and linked storage profile backend_type/name",
            },
        },
        "summary": summary_view,
        "matrix": matrix,
    }
