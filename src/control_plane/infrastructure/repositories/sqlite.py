import brotli
import hmac
import json
import logging
import re
import secrets
import sqlite3
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
    WorkerScheduleClaimRepository,
    JOB_LISTING_SORT_KEYS,
    build_job_stats,
    job_stats_day_keys,
)
from src.control_plane.domain.models import (
    BackupTargetRecord,
    CacheGenerationRecord,
    CONFIRMATION_DEADLINE_KEY,
    DashboardReadSnapshotRecord,
    JOB_UNCONFIRMED_AFTER_SECONDS,
    IndexStatusRecord,
    InventorySnapshot,
    JobLogPruneConflict,
    JobRecord,
    JobStatus,
    NotificationChannelRecord,
    READ_ONLY_SNAPSHOT_COMMANDS,
    READ_ONLY_UNCONFIRMED_LOG,
    RetentionPolicyRecord,
    SecretRecord,
    SettingsRecord,
    SnapshotRecord,
    StorageProfileRecord,
    TargetStatsRecord,
    UNCONFIRMED_OUTCOME_LOG,
    UNCONFIRMED_OUTCOME_RECOVERY,
    WorkerEnrollmentRecord,
    WorkerRecord,
    WorkerScheduleClaimRecord,
    utcnow,
)
from src.control_plane.infrastructure.sqlite_runtime import SQLiteConnection as _SQLiteConnection

logger = logging.getLogger(__name__)


LEASE_DIAGNOSTIC_CODE = "worker_lease_expired"

# T1 S1 job log archival: terminal jobs older than the policy cutoff keep
# their log text (lines plus paired timestamps) in one Brotli blob instead
# of the raw log_lines_json column. The raw column keeps its historical
# TEXT NOT NULL shape and stores the '[]' sentinel while the archive is in
# place; no automatic runtime conversion happens in T1, the caller decides.
JOB_LOG_ARCHIVE_FORMAT = "job-log-brotli-v1"
JOB_LOG_ARCHIVE_SENTINEL = "[]"
JOB_LOG_ARCHIVE_BROTLI_QUALITY = 4

# T2 S1-S3 job log lifecycle compact metadata: one small row per job with
# only the stored log byte size (raw columns plus the Brotli archive when
# present). Maintained atomically by triggers on the jobs table so every
# INSERT/replace path is covered; job-log policy usage queries aggregate
# ONLY this table, never the wide rows or their payload text. Legacy rows
# recorded before the triggers existed stay unaccounted until the bounded
# keyset backfill pass runs while maintenance is enabled.
JOB_LOG_SIZES_TABLE = "job_log_sizes"
JOB_LOG_SIZES_BACKFILL_META_KEY = "job_log_sizes_backfill_done"
JOB_LOG_POLICY_EVENTS_META_KEY = "job_log_policy_events"
# T3 S4: confirmed admin cutoff prune, persisted resumable run state.
JOB_LOG_PRUNE_STATE_META_KEY = "job_log_prune_state"
DEFAULT_JOB_LOG_PRUNE_BATCH_SIZE = 25
MAX_PRUNE_BATCH_ROWS = 200
JOB_LOG_POLICY_EVENT_LIMIT = 20

# B1: policy selection is a COVERING path over the compact metadata only.
# Wide jobs rows (log payloads living in overflow chains) made the batch-0
# jobs-join selection cost ~0.47 s at 400 rows x 2 MiB logs while tiny log
# rows stayed under 0.01 s; on a 24 GiB database that ran inside every
# BEGIN IMMEDIATE batch, so the selection now projects all needed state
# (terminal/payload/archived/removed flags, finish time) into the compact
# metadata via triggers and selects/orders only through the policy index.
# The wide jobs rows are touched again only per SELECTED candidate (bounded
# by the batch limit) for the diagnostic recheck and the writer CAS.
JOB_LOG_POLICY_INDEX_NAME = "idx_job_log_sizes_policy"
# B2: the stored quota is read inside the eviction transaction and converted
# with this fixed 1 GiB = 2**30 factor (tests scale it down).
JOB_LOG_QUOTA_GIB_TO_BYTES = 1024 ** 3


def job_log_policy_candidates_sql(
    *,
    with_cutoff: bool,
    require_unarchived: bool = False,
    exclude_count: int = 0,
) -> str:
    """Covering candidate selection over compact metadata (deterministic)."""
    clauses = [
        "payload_present = 1",
        "terminal = 1",
        "removed = 0",
    ]
    if with_cutoff:
        clauses.append("finished_at <= :cutoff")
    if require_unarchived:
        clauses.append("archived = 0")
    if exclude_count > 0:
        placeholders = ", ".join("?" for _ in range(exclude_count))
        clauses.append(f"job_id NOT IN ({placeholders})")
    where = " AND ".join(clauses)
    return (
        f"SELECT job_id FROM {JOB_LOG_SIZES_TABLE} WHERE {where} "
        "ORDER BY finished_at ASC, job_id ASC LIMIT :limit"
    )

# Terminal outcomes recognized by the compact metadata projection; the
# legacy 'cancelled' spelling stays eligible (trigger + backfill only read
# status, never the wide payload columns).
JOB_LOG_ELIGIBLE_STATUSES_SQL = "('succeeded', 'failed', 'canceled', 'cancelled')"


def _job_log_archive_eligible(
    *,
    status: Any,
    log_lines: Any,
    finished_at: Any,
    cutoff: Any,
    result_summary: Any,
) -> bool:
    """Whether a job may archive its logs at the given cutoff (T2 reuses this).

    Only terminal succeeded/failed/canceled outcomes (legacy 'cancelled'
    spelling included through normalize) finished at or before the cutoff;
    never jobs still active or awaiting confirmation, and never synthetic
    lease-expiry failures, because those stay late-write capable and may
    still receive the worker's real terminal report.
    """
    if JobStatus.normalize(status) not in (
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELED,
    ):
        return False
    if not isinstance(log_lines, list) or not log_lines:
        return False
    if isinstance(result_summary, dict) and result_summary.get("diagnostic_code") == LEASE_DIAGNOSTIC_CODE:
        return False
    finished = _dt(finished_at) if finished_at is not None else None
    if finished is None:
        return False
    if finished.tzinfo:
        finished = finished.astimezone(timezone.utc).replace(tzinfo=None)
    if cutoff.tzinfo:
        cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)
    return finished <= cutoff


def _encode_job_log_archive(log_lines: List[str], log_timestamps: Optional[List[Optional[str]]]) -> Optional[bytes]:
    """Serialize one job's logs plus aligned timestamps into a Brotli blob.

    The recoverable framing is a versioned JSON envelope holding the lines
    JSON and the timestamps JSON as strings, compressed with Brotli text
    mode at the fixed quality; no positional parsing and no duplicated raw
    copies of the lines are stored. Returns None when compression is not
    possible so the caller keeps the existing bytes untouched.
    """
    timestamps = log_timestamps
    if not (isinstance(timestamps, list) and len(timestamps) == len(log_lines)):
        timestamps = [None] * len(log_lines)
    try:
        envelope = {
            "format": JOB_LOG_ARCHIVE_FORMAT,
            "lines_json": json.dumps(log_lines),
            "timestamps_json": json.dumps(timestamps) if any(timestamps) else None,
        }
        return brotli.compress(
            json.dumps(envelope).encode("utf-8"),
            quality=JOB_LOG_ARCHIVE_BROTLI_QUALITY,
            mode=brotli.MODE_TEXT,
        )
    except Exception:
        logger.warning("Brotli job log archive encoding failed; existing storage kept")
        return None


def _decode_job_log_archive(blob: Any) -> Tuple[List[str], Optional[List[Optional[str]]]]:
    """Inverse of _encode_job_log_archive; unreadable data logs and empties."""
    try:
        envelope = json.loads(brotli.decompress(bytes(blob)))
        if not isinstance(envelope, dict) or envelope.get("format") != JOB_LOG_ARCHIVE_FORMAT:
            raise ValueError("unknown job log archive format")
        lines = _json_load(envelope.get("lines_json"), None)
        if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
            raise ValueError("job log archive lines are not text")
        timestamps_raw = envelope.get("timestamps_json")
        timestamps = _json_load(timestamps_raw, None) if isinstance(timestamps_raw, str) else None
        if not isinstance(timestamps, list) or len(timestamps) != len(lines):
            timestamps = [None] * len(lines)
        if not any(timestamps):
            timestamps = None  # Same stored convention as log_timestamps_json.
        return lines, timestamps
    except Exception:
        logger.warning("Unreadable Brotli job log archive; treating the job log as empty")
        return [], None

# Compact dashboard-stats projection: one row per job with only (job_id,
# normalized status, started_at). Maintained atomically by triggers on the
# jobs table so every INSERT/UPDATE/DELETE path is covered; jobs and logs
# themselves are never pruned, moved or rewritten.
JOB_STATS_FACTS_TABLE = "job_stats_facts"
JOB_STATS_FACTS_BACKFILL_META_KEY = "job_stats_facts_backfill_done"
LEASE_DIAGNOSTIC_CATEGORY = "worker_interrupted"
LEASE_DIAGNOSTIC_MESSAGE = (
    "Control Plane observed that the worker lease expired before a terminal result arrived; "
    "the worker or runtime cause is unknown."
)


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _json_load(value: Optional[str], fallback):
    if not value:
        return fallback
    return json.loads(value)


def _datetime_text(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        text = value.isoformat()
        return f"{text}Z" if value.utcoffset() is None else text
    return value.isoformat() if hasattr(value, "isoformat") else value if isinstance(value, str) else None


def _lease_context_for_values(
    *,
    job_id: str,
    worker_id: Optional[str],
    target_id: Optional[str],
    command: Optional[str],
    lease_issued_at: Any,
    lease_expires_at: Any,
    updated_at: Any,
    result_summary: Any,
    now: Any = None,
) -> Dict[str, Any]:
    summary = result_summary if isinstance(result_summary, dict) else {}
    current = summary.get("lease_context")
    context = dict(current) if isinstance(current, dict) else {}
    context.setdefault("job_id", job_id)
    context.setdefault("target_id", target_id)
    context.setdefault("worker_id", worker_id)
    context.setdefault("command", command)
    if lease_issued_at is not None:
        context.setdefault("lease_issued_at", _datetime_text(lease_issued_at))
    if lease_expires_at is not None:
        context.setdefault("lease_expires_at", _datetime_text(lease_expires_at))
    context.setdefault("last_repository_update_at", _datetime_text(updated_at))
    if now is not None:
        context.setdefault("observed_at", _datetime_text(now))
    return context


def _lease_diagnostics(
    *,
    job_id: str,
    worker_id: Optional[str],
    target_id: Optional[str],
    command: Optional[str],
    lease_issued_at: Any,
    lease_expires_at: Any,
    updated_at: Any,
    result_summary: Any,
    now: Any,
) -> Dict[str, Any]:
    context = _lease_context_for_values(
        job_id=job_id,
        worker_id=worker_id,
        target_id=target_id,
        command=command,
        lease_issued_at=lease_issued_at,
        lease_expires_at=lease_expires_at,
        updated_at=updated_at,
        result_summary=result_summary,
        now=now,
    )
    summary = result_summary if isinstance(result_summary, dict) else {}
    try:
        sequence = int(context.get("last_progress_sequence", summary.get("progress_sequence", 0)) or 0)
    except (TypeError, ValueError):
        sequence = 0
    observed = {
        "job_id": context.get("job_id") or job_id,
        "target_id": context.get("target_id", target_id),
        "worker_id": context.get("worker_id") or worker_id,
        "command": context.get("command") or command,
        "lease_issued_at": context.get("lease_issued_at"),
        "lease_expires_at": context.get("lease_expires_at"),
        "last_repository_update_at": context.get("last_repository_update_at") or _datetime_text(updated_at),
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


def _renewal_cleared_summary_keys() -> tuple:
    """Diagnostic keys removed when the owner renews a lease after expiry."""
    return (
        "diagnostic_code",
        "diagnostic_category",
        "diagnostic_message",
        "lease_diagnostics",
        "recovery",
        CONFIRMATION_DEADLINE_KEY,
    )


def _confirmation_deadline(summary: Any, updated_at: Optional[datetime]) -> Optional[datetime]:
    """Deadline after which an awaiting-confirmation outcome becomes unknown.

    Falls back to the diagnostic write time for jobs stored before the stored
    deadline existed; the fallback equals the original lease expiry plus the
    same 24-hour window, and any owner progress afterwards refreshes it.
    """
    raw = summary.get(CONFIRMATION_DEADLINE_KEY) if isinstance(summary, dict) else None
    if isinstance(raw, str) and raw:
        try:
            deadline = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if deadline.tzinfo:
                deadline = deadline.astimezone(timezone.utc).replace(tzinfo=None)
            return deadline
        except (TypeError, ValueError, OverflowError):
            pass
    if updated_at is None:
        return None
    if updated_at.tzinfo:
        updated_at = updated_at.astimezone(timezone.utc).replace(tzinfo=None)
    return updated_at + timedelta(seconds=JOB_UNCONFIRMED_AFTER_SECONDS)


class SQLiteRepositoryBase:
    SCHEMA_VERSION = 1

    def __init__(self, database_path: str):
        self.database_path = database_path
        self._lock = Lock()
        self._initialize()

    def _connect(self):
        return _SQLiteConnection(self.database_path)

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    host_name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT
                );

                CREATE TABLE IF NOT EXISTS worker_enrollments (
                    id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    token_hash TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    host_name TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );

                CREATE TABLE IF NOT EXISTS inventories (
                    worker_id TEXT PRIMARY KEY,
                    inventory_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS targets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    runtime_type TEXT NOT NULL DEFAULT 'docker',
                    namespace TEXT,
                    pvc_names_json TEXT NOT NULL DEFAULT '[]',
                    filesystem_paths_json TEXT NOT NULL DEFAULT '[]',
                    compose_project TEXT,
                    volume_targets_json TEXT NOT NULL,
                    backup_mode TEXT NOT NULL,
                    backup_strategy TEXT NOT NULL,
                    runtime_image TEXT,
                    runtime_command TEXT,
                    runtime_environment_json TEXT NOT NULL,
                    runtime_volumes_json TEXT NOT NULL,
                    runtime_network_mode TEXT,
                    storage_profile_id TEXT,
                    path_storage TEXT,
                    retention_policy_id TEXT,
                    execution_policy_id TEXT,
                    restic_password_secret_id TEXT,
                    restore_defaults_json TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    live_access_enabled INTEGER NOT NULL DEFAULT 0,
                    cron_expression TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    target_id TEXT,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner_worker_id TEXT,
                    lease_token TEXT,
                    lease_issued_at TEXT,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    result_summary_json TEXT,
                    log_lines_json TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS storage_profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    backend_type TEXT NOT NULL,
                    environment_json TEXT NOT NULL,
                    secret_refs_json TEXT NOT NULL,
                    file_secret_refs_json TEXT NOT NULL,
                    runtime_volumes_json TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS secrets (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    secret_type TEXT NOT NULL,
                    ciphertext TEXT NOT NULL,
                    key_version TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notification_channels (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    channel_type TEXT NOT NULL,
                    config_ciphertext TEXT NOT NULL,
                    key_version TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notification_delivery_attempts (
                    job_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, channel_id)
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY,
                    target_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    hostname TEXT,
                    paths_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS retention_policies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    keep_last INTEGER,
                    keep_hourly INTEGER,
                    keep_daily INTEGER,
                    keep_weekly INTEGER,
                    keep_monthly INTEGER,
                    keep_yearly INTEGER,
                    prune INTEGER NOT NULL,
                    labels_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS target_stats (
                    id TEXT PRIMARY KEY,
                    target_id TEXT UNIQUE NOT NULL,
                    worker_id TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    id TEXT PRIMARY KEY,
                    restic_repository_base TEXT NOT NULL DEFAULT '',
                    restic_password_secret_id TEXT,
                    rclone_conf_secret_id TEXT,
                    global_cron_expression TEXT,
                    control_plane_public_url TEXT NOT NULL DEFAULT '',
                    snapshot_explorer_listing_max_output_bytes INTEGER NOT NULL DEFAULT 4194304,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS worker_schedule_claims (
                    worker_id TEXT PRIMARY KEY,
                    revision TEXT NOT NULL,
                    target_ids_json TEXT NOT NULL,
                    errors_json TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dashboard_read_snapshots (
                    storage_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    tz_offset_minutes INTEGER,
                    payload_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS explorer_cache_generations (
                    target_id TEXT NOT NULL,
                    repository_fingerprint TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (target_id, repository_fingerprint)
                );

                CREATE TABLE IF NOT EXISTS explorer_index_status (
                    target_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    entry_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (target_id, snapshot_id)
                );
                """
            )
            self._ensure_column(connection, "settings", "rclone_conf_secret_id", "TEXT")
            self._ensure_column(connection, "settings", "global_cron_expression", "TEXT")
            self._ensure_column(connection, "settings", "control_plane_public_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(
                connection,
                "settings",
                "snapshot_explorer_listing_max_output_bytes",
                "INTEGER NOT NULL DEFAULT 4194304",
            )
            self._ensure_column(
                connection,
                "settings",
                "job_log_maintenance_enabled",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection,
                "settings",
                "job_log_archive_after_days",
                f"INTEGER NOT NULL DEFAULT {SettingsRecord.DEFAULT_JOB_LOG_ARCHIVE_AFTER_DAYS}",
            )
            self._ensure_column(
                connection,
                "settings",
                "job_log_retention_days",
                f"INTEGER NOT NULL DEFAULT {SettingsRecord.DEFAULT_JOB_LOG_RETENTION_DAYS}",
            )
            self._ensure_column(connection, "settings", "job_log_quota_gib", "INTEGER")
            # T3 S5 presence-aware migration: only a previously ACTIVE legacy
            # quota (maintenance enabled AND configured) becomes explicitly
            # enabled; every other stored combination is materialized dormant
            # so old overdue data never starts disappearing automatically.
            quota_flag_preexisting = "job_log_quota_enabled" in {
                row["name"] for row in connection.execute("PRAGMA table_info(settings)").fetchall()
            }
            self._ensure_column(connection, "settings", "job_log_quota_enabled", "INTEGER")
            if not quota_flag_preexisting:
                connection.execute(
                    "UPDATE settings SET job_log_quota_enabled = "
                    "CASE WHEN job_log_maintenance_enabled = 1 "
                    "AND job_log_quota_gib IS NOT NULL AND job_log_quota_gib >= 1 "
                    "THEN 1 ELSE 0 END"
                )
            self._ensure_column(connection, "jobs", "log_timestamps_json", "TEXT")
            self._ensure_column(connection, "jobs", "job_log_brotli", "BLOB")
            self._ensure_column(connection, "jobs", "job_log_removed", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "targets", "cron_expression", "TEXT")
            self._ensure_column(connection, "targets", "live_access_enabled", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "targets", "path_storage", "TEXT")
            self._ensure_column(connection, "targets", "runtime_type", "TEXT NOT NULL DEFAULT 'docker'")
            self._ensure_column(connection, "targets", "namespace", "TEXT")
            self._ensure_column(connection, "targets", "pvc_names_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(connection, "targets", "filesystem_paths_json", "TEXT NOT NULL DEFAULT '[]'")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_submitted_at_id_desc "
                "ON jobs (submitted_at DESC, id DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_lease_expires_at "
                "ON jobs (status, lease_expires_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_target_id_submitted_at_desc "
                "ON jobs (target_id, submitted_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status_submitted_at_desc "
                "ON jobs (status, submitted_at DESC)"
            )
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {JOB_STATS_FACTS_TABLE} ("
                "job_id TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT)"
            )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{JOB_STATS_FACTS_TABLE}_status "
                f"ON {JOB_STATS_FACTS_TABLE} (status)"
            )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{JOB_STATS_FACTS_TABLE}_started_at_status "
                f"ON {JOB_STATS_FACTS_TABLE} (started_at, status)"
            )
            for trigger_sql in (
                "CREATE TRIGGER IF NOT EXISTS trg_job_stats_facts_insert "
                f"AFTER INSERT ON jobs BEGIN INSERT OR REPLACE INTO {JOB_STATS_FACTS_TABLE} "
                "(job_id, status, started_at) VALUES "
                "(NEW.id, CASE NEW.status WHEN 'cancelled' THEN 'canceled' ELSE NEW.status END, "
                "NEW.started_at); END",
                "CREATE TRIGGER IF NOT EXISTS trg_job_stats_facts_update "
                f"AFTER UPDATE OF status, started_at ON jobs BEGIN INSERT OR REPLACE INTO {JOB_STATS_FACTS_TABLE} "
                "(job_id, status, started_at) VALUES "
                "(NEW.id, CASE NEW.status WHEN 'cancelled' THEN 'canceled' ELSE NEW.status END, "
                "NEW.started_at); END",
                "CREATE TRIGGER IF NOT EXISTS trg_job_stats_facts_delete "
                f"AFTER DELETE ON jobs BEGIN DELETE FROM {JOB_STATS_FACTS_TABLE} "
                "WHERE job_id = OLD.id; END",
            ):
                connection.execute(trigger_sql)
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {JOB_LOG_SIZES_TABLE} ("
                "job_id TEXT PRIMARY KEY, log_bytes INTEGER NOT NULL, "
                "finished_at TEXT, terminal INTEGER, payload_present INTEGER, "
                "archived INTEGER, removed INTEGER)"
            )
            # B1: compact covering index for policy selection; small rows only,
            # no wide column in the index and no expensive startup build over
            # jobs payloads.
            for compact_column, column_sql in (
                ("finished_at", "TEXT"),
                ("terminal", "INTEGER"),
                ("payload_present", "INTEGER"),
                ("archived", "INTEGER"),
                ("removed", "INTEGER"),
            ):
                self._ensure_column(connection, JOB_LOG_SIZES_TABLE, compact_column, column_sql)
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS {JOB_LOG_POLICY_INDEX_NAME} "
                f"ON {JOB_LOG_SIZES_TABLE} "
                "(payload_present, terminal, removed, finished_at, job_id)"
            )
            job_log_terminal_expr = (
                f"CASE WHEN NEW.status IN {JOB_LOG_ELIGIBLE_STATUSES_SQL} THEN 1 ELSE 0 END"
            )
            job_log_payload_expr = (
                "CASE WHEN NEW.job_log_brotli IS NOT NULL THEN 1 "
                "WHEN NEW.log_lines_json IS NULL OR NEW.log_lines_json IN ('', '[]') "
                "THEN 0 ELSE 1 END"
            )
            job_log_columns = (
                "(job_id, log_bytes, finished_at, terminal, payload_present, archived, removed)"
            )
            job_log_values = (
                "(NEW.id, "
                + "length(CAST(NEW.log_lines_json AS BLOB))"
                + " + COALESCE(length(CAST(NEW.log_timestamps_json AS BLOB)), 0)"
                + " + COALESCE(length(NEW.job_log_brotli), 0)"
                + ", NEW.finished_at, " + job_log_terminal_expr
                + ", " + job_log_payload_expr
                + ", CASE WHEN NEW.job_log_brotli IS NOT NULL THEN 1 ELSE 0 END, "
                + "COALESCE(NEW.job_log_removed, 0))"
            )
            for size_trigger_sql in (
                "CREATE TRIGGER IF NOT EXISTS trg_job_log_sizes_insert "
                f"AFTER INSERT ON jobs BEGIN INSERT OR REPLACE INTO {JOB_LOG_SIZES_TABLE} "
                + job_log_columns + " VALUES " + job_log_values + "; END",
                "CREATE TRIGGER IF NOT EXISTS trg_job_log_sizes_update "
                f"AFTER UPDATE OF log_lines_json, log_timestamps_json, job_log_brotli, "
                "job_log_removed, status, finished_at ON jobs "
                f"BEGIN INSERT OR REPLACE INTO {JOB_LOG_SIZES_TABLE} "
                + job_log_columns + " VALUES " + job_log_values + "; END",
                "CREATE TRIGGER IF NOT EXISTS trg_job_log_sizes_delete "
                f"AFTER DELETE ON jobs BEGIN DELETE FROM {JOB_LOG_SIZES_TABLE} WHERE job_id = OLD.id; END",
            ):
                connection.execute(size_trigger_sql)
            # B1: a batch-0 marker must not claim completeness while any
            # compact row lacks the projected flags; the check is a scan of
            # the compact metadata table only (small rows, no payload deref).
            stale_flags = connection.execute(
                f"SELECT 1 FROM {JOB_LOG_SIZES_TABLE} WHERE terminal IS NULL LIMIT 1"
            ).fetchone()
            if stale_flags:
                connection.execute(
                    "DELETE FROM schema_meta WHERE key = ?",
                    (JOB_LOG_SIZES_BACKFILL_META_KEY,),
                )
            self._backfill_job_stats_facts_once(connection)
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )

    def get_schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table_name: str, column_name: str, column_sql: str) -> None:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {row["name"] for row in rows}
        if column_name not in existing:
            connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")

    def _backfill_job_stats_facts_once(self, connection: sqlite3.Connection) -> None:
        """Additively backfill the stats projection from existing history.

        Idempotent: the marker lives in schema_meta and is re-checked inside a
        BEGIN IMMEDIATE transaction, so repeated initializations of the same
        database (other processes included) can never duplicate the backfill
        or double-count totals.
        """
        marker = connection.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            (JOB_STATS_FACTS_BACKFILL_META_KEY,),
        ).fetchone()
        if marker:
            return
        try:
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            marker = connection.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (JOB_STATS_FACTS_BACKFILL_META_KEY,),
            ).fetchone()
            if not marker:
                connection.execute(
                    f"""
                    INSERT OR REPLACE INTO {JOB_STATS_FACTS_TABLE} (job_id, status, started_at)
                    SELECT id, CASE status WHEN 'cancelled' THEN 'canceled' ELSE status END, started_at
                    FROM jobs
                    WHERE id NOT IN (SELECT job_id FROM {JOB_STATS_FACTS_TABLE})
                    """
                )
                connection.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                    (
                        JOB_STATS_FACTS_BACKFILL_META_KEY,
                        f"v{self.SCHEMA_VERSION}:{datetime.utcnow().isoformat()}",
                    ),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


class SQLiteWorkerRepository(SQLiteRepositoryBase, WorkerRepository):
    def save(self, worker: WorkerRecord) -> WorkerRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO workers (
                    id, name, host_name, version, labels_json, status, created_at, updated_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    worker.id,
                    worker.name,
                    worker.host_name,
                    worker.version,
                    json.dumps(worker.labels),
                    worker.status,
                    worker.created_at.isoformat(),
                    worker.updated_at.isoformat(),
                    worker.last_seen_at.isoformat() if worker.last_seen_at else None,
                ),
            )
        return worker

    def get(self, worker_id: str) -> Optional[WorkerRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
        return self._row_to_worker(row) if row else None

    def find_by_name(self, name: str) -> Optional[WorkerRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM workers WHERE name = ? ORDER BY created_at ASC LIMIT 1", (name,)).fetchone()
        return self._row_to_worker(row) if row else None

    def list(self) -> List[WorkerRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM workers ORDER BY created_at ASC").fetchall()
        return [self._row_to_worker(row) for row in rows]

    def delete(self, worker_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM workers WHERE id = ?", (worker_id,))
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_worker(row: sqlite3.Row) -> WorkerRecord:
        return WorkerRecord(
            id=row["id"],
            name=row["name"],
            host_name=row["host_name"],
            version=row["version"],
            labels=_json_load(row["labels_json"], {}),
            status=row["status"],
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
            last_seen_at=_dt(row["last_seen_at"]),
        )

class SQLiteWorkerEnrollmentRepository(SQLiteRepositoryBase, WorkerEnrollmentRepository):
    def save(self, enrollment: WorkerEnrollmentRecord) -> WorkerEnrollmentRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO worker_enrollments (
                    id, worker_id, token_hash, name, host_name, labels_json, created_at, expires_at, used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    enrollment.id,
                    enrollment.worker_id,
                    enrollment.token_hash,
                    enrollment.name,
                    enrollment.host_name,
                    json.dumps(enrollment.labels),
                    enrollment.created_at.isoformat(),
                    enrollment.expires_at.isoformat(),
                    enrollment.used_at.isoformat() if enrollment.used_at else None,
                ),
            )
        return enrollment

    def get(self, enrollment_id: str) -> Optional[WorkerEnrollmentRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM worker_enrollments WHERE id = ?", (enrollment_id,)).fetchone()
        return self._row_to_enrollment(row) if row else None

    def get_by_token_hash(self, token_hash: str) -> Optional[WorkerEnrollmentRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worker_enrollments WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return self._row_to_enrollment(row) if row else None

    def list(self) -> List[WorkerEnrollmentRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM worker_enrollments ORDER BY created_at DESC").fetchall()
        return [self._row_to_enrollment(row) for row in rows]

    @staticmethod
    def _row_to_enrollment(row: sqlite3.Row) -> WorkerEnrollmentRecord:
        return WorkerEnrollmentRecord(
            id=row["id"],
            worker_id=row["worker_id"],
            token_hash=row["token_hash"],
            name=row["name"],
            host_name=row["host_name"],
            labels=_json_load(row["labels_json"], {}),
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            expires_at=_dt(row["expires_at"]) or datetime.utcnow(),
            used_at=_dt(row["used_at"]),
        )


class SQLiteInventoryRepository(SQLiteRepositoryBase, InventoryRepository):
    def save(self, snapshot: InventorySnapshot) -> InventorySnapshot:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO inventories (
                    worker_id, inventory_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    snapshot.worker_id,
                    json.dumps(snapshot.inventory),
                    snapshot.created_at.isoformat(),
                    snapshot.updated_at.isoformat(),
                ),
            )
        return snapshot

    def get_by_worker(self, worker_id: str) -> Optional[InventorySnapshot]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM inventories WHERE worker_id = ?", (worker_id,)).fetchone()
        if not row:
            return None
        return InventorySnapshot(
            worker_id=row["worker_id"],
            inventory=_json_load(row["inventory_json"], {}),
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )

    def delete_by_worker(self, worker_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM inventories WHERE worker_id = ?", (worker_id,))
            return cursor.rowcount > 0


class SQLiteTargetRepository(SQLiteRepositoryBase, TargetRepository):
    def save(self, target: BackupTargetRecord) -> BackupTargetRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO targets (
                    id, name, worker_id, runtime_type, namespace, pvc_names_json, filesystem_paths_json, compose_project, volume_targets_json, backup_mode, backup_strategy,
                    runtime_image, runtime_command, runtime_environment_json, runtime_volumes_json, runtime_network_mode,
                    storage_profile_id, path_storage, retention_policy_id, execution_policy_id, restic_password_secret_id, restore_defaults_json, labels_json,
                    enabled, live_access_enabled, cron_expression, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    target.id,
                    target.name,
                    target.worker_id,
                    target.runtime_type,
                    target.namespace,
                    json.dumps(target.pvc_names),
                    json.dumps(target.filesystem_paths),
                    target.compose_project,
                    json.dumps(target.volume_targets),
                    target.backup_mode,
                    target.backup_strategy,
                    target.runtime_image,
                    target.runtime_command,
                    json.dumps(target.runtime_environment),
                    json.dumps(target.runtime_volumes),
                    target.runtime_network_mode,
                    target.storage_profile_id,
                    target.path_storage,
                    target.retention_policy_id,
                    target.execution_policy_id,
                    target.restic_password_secret_id,
                    json.dumps(target.restore_defaults),
                    json.dumps(target.labels),
                    1 if target.enabled else 0,
                    1 if target.live_access_enabled else 0,
                    target.cron_expression,
                    target.created_at.isoformat(),
                    target.updated_at.isoformat(),
                ),
            )
        return target

    def get(self, target_id: str) -> Optional[BackupTargetRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
        return self._row_to_target(row) if row else None

    def list(self) -> List[BackupTargetRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM targets ORDER BY created_at ASC").fetchall()
        return [self._row_to_target(row) for row in rows]

    def delete(self, target_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM targets WHERE id = ?", (target_id,))
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_target(row: sqlite3.Row) -> BackupTargetRecord:
        return BackupTargetRecord(
            id=row["id"],
            name=row["name"],
            worker_id=row["worker_id"],
            runtime_type=row["runtime_type"] if "runtime_type" in row.keys() else "docker",
            namespace=row["namespace"] if "namespace" in row.keys() else None,
            pvc_names=_json_load(row["pvc_names_json"], []) if "pvc_names_json" in row.keys() else [],
            filesystem_paths=_json_load(row["filesystem_paths_json"], []) if "filesystem_paths_json" in row.keys() else [],
            compose_project=row["compose_project"],
            volume_targets=_json_load(row["volume_targets_json"], []),
            backup_mode=row["backup_mode"],
            backup_strategy=row["backup_strategy"],
            runtime_image=row["runtime_image"],
            runtime_command=row["runtime_command"],
            runtime_environment=_json_load(row["runtime_environment_json"], {}),
            runtime_volumes=_json_load(row["runtime_volumes_json"], {}),
            runtime_network_mode=row["runtime_network_mode"],
            storage_profile_id=row["storage_profile_id"],
            path_storage=row["path_storage"] if "path_storage" in row.keys() else None,
            retention_policy_id=row["retention_policy_id"],
            execution_policy_id=row["execution_policy_id"],
            restic_password_secret_id=row["restic_password_secret_id"],
            restore_defaults=_json_load(row["restore_defaults_json"], {}),
            labels=_json_load(row["labels_json"], {}),
            enabled=bool(row["enabled"]),
            live_access_enabled=bool(row["live_access_enabled"]) if "live_access_enabled" in row.keys() else False,
            cron_expression=row["cron_expression"] if "cron_expression" in row.keys() else None,
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )


class SQLiteJobRepository(SQLiteRepositoryBase, JobRepository):
    MAX_LOG_LINES = 1000
    MAX_LOG_CHARS = 512 * 1024

    def save(self, job: JobRecord) -> JobRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO jobs (
                    id, worker_id, command, payload_json, requested_by, target_id, trigger, status,
                    owner_worker_id, lease_token, lease_issued_at, lease_expires_at, attempt_count,
                    result_summary_json, log_lines_json, log_timestamps_json, job_log_brotli,
                    job_log_removed, submitted_at, started_at, finished_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.worker_id,
                    job.command,
                    json.dumps(job.payload),
                    job.requested_by,
                    job.target_id,
                    job.trigger,
                    JobStatus.normalize(job.status),
                    job.owner_worker_id,
                    job.lease_token,
                    job.lease_issued_at.isoformat() if job.lease_issued_at else None,
                    job.lease_expires_at.isoformat() if job.lease_expires_at else None,
                    job.attempt_count,
                    json.dumps(job.result_summary) if job.result_summary is not None else None,
                    json.dumps(job.log_lines),
                    json.dumps(job.log_timestamps)
                    if isinstance(job.log_timestamps, list) and len(job.log_timestamps) == len(job.log_lines)
                    else None,
                    # save() is the authoritative full-record replace: any stored
                    # Brotli archive is replaced by these raw log columns, so a
                    # stale archive is never kept beside a rewritten raw log.
                    None,
                    1 if getattr(job, "job_log_removed", False) else 0,
                    job.submitted_at.isoformat(),
                    job.started_at.isoformat() if job.started_at else None,
                    job.finished_at.isoformat() if job.finished_at else None,
                    job.updated_at.isoformat(),
                ),
            )
            job.status = JobStatus.normalize(job.status)
        return job

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def list(self) -> List[JobRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY submitted_at DESC").fetchall()
        return [self._row_to_job(row) for row in rows]

    def archive_job_log(self, job_id: str, cutoff: datetime) -> int:
        """Archive one finished job's logs as Brotli (T1 S1), returning bytes saved.

        The Brotli payload is produced outside the SQLite write lock; the
        final write is a single compare-and-swap against the observed log
        strings, status, finish time and updated_at, so a later progress or
        terminal write is never dropped. On any failed validation the stored
        bytes stay untouched and the return is 0 (including "not smaller":
        an archive is only kept when it actually saves bytes).
        """
        with self._connect() as connection:
            snapshot = connection.execute(
                """
                SELECT status, log_lines_json, log_timestamps_json, finished_at,
                       result_summary_json, updated_at
                FROM jobs
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
        if snapshot is None:
            return 0
        log_lines_json = snapshot["log_lines_json"]
        timestamps_json = snapshot["log_timestamps_json"]
        # The '[]' sentinel is either an already-archived row or an empty log:
        # both are ineligible, and empty text cannot ever save bytes.
        if not isinstance(log_lines_json, str) or not log_lines_json or log_lines_json == JOB_LOG_ARCHIVE_SENTINEL:
            return 0
        try:
            log_lines = _json_load(log_lines_json, None)
            log_timestamps = _json_load(timestamps_json, None) if timestamps_json is not None else None
            result_summary = _json_load(snapshot["result_summary_json"], None)
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0
        if not _job_log_archive_eligible(
            status=snapshot["status"],
            log_lines=log_lines,
            finished_at=snapshot["finished_at"],
            cutoff=cutoff,
            result_summary=result_summary,
        ):
            return 0
        timestamps = (
            log_timestamps
            if isinstance(log_timestamps, list) and len(log_timestamps) == len(log_lines)
            else [None] * len(log_lines)
        )
        archive = _encode_job_log_archive(log_lines, timestamps)
        if archive is None:
            return 0
        try:
            raw_chars = (
                len(log_lines_json.encode("utf-8"))
                + (len(timestamps_json.encode("utf-8")) if isinstance(timestamps_json, str) else 0)
            )
            saved = raw_chars - len(archive)
        except (TypeError, ValueError):
            return 0
        if saved <= 0:
            return 0
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET log_lines_json = ?, log_timestamps_json = NULL, job_log_brotli = ?
                    WHERE id = ? AND status = ?
                      AND log_lines_json IS ?
                      AND log_timestamps_json IS ?
                      AND finished_at IS ?
                      AND updated_at IS ?
                    """,
                    (
                        JOB_LOG_ARCHIVE_SENTINEL,
                        archive,
                        job_id,
                        snapshot["status"],
                        log_lines_json,
                        timestamps_json if timestamps_json is not None else None,
                        snapshot["finished_at"],
                        snapshot["updated_at"],
                    ),
                )
                connection.commit()
                return saved if updated.rowcount == 1 else 0
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    # ------------------------------------------------------------------
    # T2 S1-S3 job log lifecycle: bounded policy operations. Only this
    # SQLite-backed repository implements them; the in-memory backend does
    # not, so maintenance is reported as unsupported there. Every write is
    # an atomic predicate update inside one short BEGIN IMMEDIATE
    # transaction; a failure leaves the stored log bytes untouched.
    # ------------------------------------------------------------------

    @staticmethod
    def _log_backfill_row(connection, after_id: Any = "", limit: int = 1) -> Tuple[List[str], bool]:
        """One bounded keyset batch of compact metadata rows needing refresh.

        Missing compact rows and batch-0 rows without the projected policy
        flags are both refreshed here. Only the selected (bounded) batch has
        its wide columns touched; the policy-selection path never does. The
        raw log payload text is never decoded and never leaves the database.
        The batch is complete when fewer than `limit` pending rows were found
        from the keyset position.
        """
        rows = connection.execute(
            f"""
            SELECT j.id,
                   length(CAST(j.log_lines_json AS BLOB))
                   + COALESCE(length(CAST(j.log_timestamps_json AS BLOB)), 0)
                   + COALESCE(length(j.job_log_brotli), 0) AS log_bytes,
                   j.finished_at AS finished_at,
                   CASE WHEN j.status IN {JOB_LOG_ELIGIBLE_STATUSES_SQL} THEN 1 ELSE 0 END AS terminal,
                   CASE WHEN j.job_log_brotli IS NOT NULL THEN 1
                        WHEN j.log_lines_json IS NULL OR j.log_lines_json IN ('', '[]')
                        THEN 0 ELSE 1 END AS payload_present,
                   CASE WHEN j.job_log_brotli IS NOT NULL THEN 1 ELSE 0 END AS archived,
                   COALESCE(j.job_log_removed, 0) AS removed
            FROM jobs j LEFT JOIN {JOB_LOG_SIZES_TABLE} s ON s.job_id = j.id
            WHERE (s.job_id IS NULL OR s.terminal IS NULL) AND j.id > ?
            ORDER BY j.id ASC
            LIMIT ?
            """,
            (after_id, limit),
        ).fetchall()
        for row in rows:
            connection.execute(
                f"INSERT OR REPLACE INTO {JOB_LOG_SIZES_TABLE} "
                "(job_id, log_bytes, finished_at, terminal, payload_present, archived, removed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["id"],
                    row["log_bytes"],
                    row["finished_at"],
                    row["terminal"],
                    row["payload_present"],
                    row["archived"],
                    row["removed"],
                ),
            )
        last_id = rows[-1]["id"] if rows else after_id
        followup = connection.execute(
            f"""
            SELECT 1
            FROM jobs j LEFT JOIN {JOB_LOG_SIZES_TABLE} s ON s.job_id = j.id
            WHERE (s.job_id IS NULL OR s.terminal IS NULL) AND j.id > ? LIMIT 1
            """,
            (last_id,),
        ).fetchone()
        return [row["id"] for row in rows], followup is None

    def backfill_job_log_sizes(self, batch_size: int = 25) -> Dict[str, Any]:
        """Refresh a bounded keyset batch of legacy compact-metadata rows.

        Covers both missing size rows and batch-0 rows whose projected policy
        flags were never set. Runs only while job-log maintenance is enabled.
        When a pass finds nothing left from the keyset position, the
        completion marker is written and usage is considered complete.
        """
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                marker = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = ?", (JOB_LOG_SIZES_BACKFILL_META_KEY,)
                ).fetchone()
                if marker:
                    connection.commit()
                    return {"backfilled": 0, "complete": True}
                backfilled, complete = self._log_backfill_row(
                    connection, after_id="", limit=batch_size
                )
                if complete:
                    connection.execute(
                        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                        (JOB_LOG_SIZES_BACKFILL_META_KEY, utcnow().isoformat()),
                    )
                connection.commit()
                return {"backfilled": len(backfilled), "complete": complete}
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def job_log_usage(self) -> Dict[str, Any]:
        """Measured job-log payload usage from the compact sizes metadata.

        The total is a hard lower bound of the real usage while the backfill
        completion marker is absent, so exceedance against a quota is always
        proven, never estimated. Legacy unaccounted rows are lazily filled by
        bounded backfill batches, never by a startup full-table scan.
        """
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COALESCE(SUM(log_bytes), 0) AS measured, COUNT(*) AS accounted "
                f"FROM {JOB_LOG_SIZES_TABLE}"
            ).fetchone()
            marker = connection.execute(
                "SELECT 1 FROM schema_meta WHERE key = ?", (JOB_LOG_SIZES_BACKFILL_META_KEY,)
            ).fetchone()
        return {
            "measured_bytes": int(row["measured"]),
            "accounted_rows": int(row["accounted"]),
            "complete": marker is not None,
        }

    @staticmethod
    def _selected_candidate_ids_locked(
        connection,
        *,
        with_cutoff: bool,
        cutoff_text: Optional[str] = None,
        require_unarchived: bool = False,
        limit: int = 1,
        exclude_ids: Optional[List[str]] = None,
    ) -> List[str]:
        """Bounded oldest-finished candidate ids from compact metadata ONLY.

        Covering (B1): the selection reads the policy index over the compact
        metadata; the wide jobs rows are never dereferenced here. Eligibility
        mirrors _job_log_archive_eligible minus the diagnostic guard: terminal
        outcomes (legacy 'cancelled' spelling included via the trigger), a
        stored finish time, present payload, never already-removed rows. The
        synthetic lease-expiry guard is rechecked per SELECTED row below, so
        result_summary_json is never scanned for every wide row.
        """
        excluded = list(dict.fromkeys(exclude_ids or ()))
        sql = job_log_policy_candidates_sql(
            with_cutoff=with_cutoff,
            require_unarchived=require_unarchived,
            exclude_count=len(excluded),
        )
        parameters: List[Any] = ([cutoff_text] if with_cutoff else []) + excluded + [max(1, limit)]
        rows = connection.execute(sql, parameters).fetchall()
        return [row["job_id"] for row in rows]

    @staticmethod
    def _policy_removal_locked(
        connection,
        job_id: str,
        *,
        cutoff_text: Optional[str] = None,
        cutoff_inclusive: bool = True,
    ) -> Tuple[str, int]:
        """Recheck one selected candidate and remove its payload via CAS.

        Bounded per-row touching: only the row currently under consideration
        is read, and only here. The recheck revalidates the live policy state
        (terminal status, stored finish time inside the cutoff, the synthetic
        lease-expiry diagnostic guard and payload presence) before the atomic
        write predicate, so a concurrent change leaves the stored bytes
        untouched. Returns ("removed"|"skipped", freed_bytes); freed bytes
        come from the observed compact size, never from a payload re-scan.
        """
        row = connection.execute(
            """
            SELECT status, finished_at, result_summary_json,
                   log_lines_json, log_timestamps_json, job_log_brotli,
                   COALESCE(job_log_removed, 0) AS removed
            FROM jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None or row["removed"]:
            return ("skipped", 0)
        if row["status"] not in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED, "cancelled"):
            return ("skipped", 0)
        if row["finished_at"] is None:
            return ("skipped", 0)
        if cutoff_text is not None:
            # A1: the prune uses the exclusive next midnight with a strict cut
            # (`<`); the retention/quota phases keep the historical inclusive
            # boundary (`<=`) so their T2 semantics never change.
            finished_text = str(row["finished_at"])
            if cutoff_inclusive:
                if finished_text > cutoff_text:
                    return ("skipped", 0)
            elif finished_text >= cutoff_text:
                return ("skipped", 0)
        try:
            summary = _json_load(row["result_summary_json"], None)
        except (TypeError, ValueError, json.JSONDecodeError):
            summary = None
        if isinstance(summary, dict) and summary.get("diagnostic_code") == LEASE_DIAGNOSTIC_CODE:
            # Late-report-capable synthetic failures keep their logs.
            return ("skipped", 0)
        size_row = connection.execute(
            f"SELECT log_bytes FROM {JOB_LOG_SIZES_TABLE} WHERE job_id = ?", (job_id,)
        ).fetchone()
        freed = int(size_row["log_bytes"]) if size_row else 0
        updated = connection.execute(
            """
            UPDATE jobs
            SET log_lines_json = ?, log_timestamps_json = NULL, job_log_brotli = NULL,
                job_log_removed = 1
            WHERE id = ? AND status IS ? AND finished_at IS ?
              AND log_lines_json IS ? AND log_timestamps_json IS ?
              AND job_log_brotli IS ? AND COALESCE(job_log_removed, 0) = 0
            """,
            (
                JOB_LOG_ARCHIVE_SENTINEL,
                job_id,
                row["status"],
                row["finished_at"],
                row["log_lines_json"],
                row["log_timestamps_json"],
                row["job_log_brotli"],
            ),
        )
        return ("removed", freed) if updated.rowcount == 1 else ("skipped", 0)

    @staticmethod
    def _measured_locked(connection) -> int:
        """Current compact total, metadata only, inside the same transaction."""
        row = connection.execute(
            f"SELECT COALESCE(SUM(log_bytes), 0) AS measured FROM {JOB_LOG_SIZES_TABLE}"
        ).fetchone()
        return int(row["measured"])

    def _policy_metadata_for(self, job_id: str) -> Dict[str, Any]:
        """Compact metadata row for one job (diagnostics/tests only)."""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM {JOB_LOG_SIZES_TABLE} WHERE job_id = ?", (job_id,)
            ).fetchone()
        return {key: row[key] for key in row.keys()} if row is not None else {}

    def expire_job_logs(
        self,
        cutoff: datetime,
        limit: int = 25,
        exclude_ids: Optional[set] = None,
    ) -> Dict[str, Any]:
        """Remove stored log payloads of finished jobs older than the cutoff (S2).

        Job rows, result summaries, snapshots and backups are never deleted:
        only the log columns are reset to the empty sentinel and the
        removed-by-policy marker is set. Deterministic order (finished_at,
        id) keeps eviction decisions reproducible from the compact policy
        index; selection never dereferences wide jobs rows, and only the
        bounded selected batch (at most `limit` rows) is rechecked and
        written inside this short writer transaction. Eligible jobs beyond
        the batch limit stay candidates for later passes; rows filtered out
        by the per-row recheck are returned in `skipped` so the caller can
        cool them down instead of retrying them every tick.
        """
        cutoff_text = str(cutoff.isoformat() if hasattr(cutoff, "isoformat") else cutoff)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                ids = self._selected_candidate_ids_locked(
                    connection,
                    with_cutoff=True,
                    cutoff_text=cutoff_text,
                    limit=max(1, limit),
                    exclude_ids=list(exclude_ids or ()),
                )
                removed: List[str] = []
                skipped: List[str] = []
                freed_total = 0
                for job_id in ids:
                    outcome, freed = self._policy_removal_locked(
                        connection, job_id, cutoff_text=cutoff_text
                    )
                    if outcome == "removed":
                        removed.append(job_id)
                        freed_total += freed
                    else:
                        skipped.append(job_id)
                connection.commit()
                return {
                    "expired": len(removed),
                    "bytes_freed": freed_total,
                    "jobs": removed,
                    "skipped": skipped,
                }
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def archive_job_log_candidates(
        self,
        cutoff: datetime,
        limit: int = 25,
        exclude_ids: Optional[set] = None,
    ) -> List[str]:
        """Bounded oldest-first candidates for lossless Brotli archival (S1).

        Covering compact-metadata selection (never-archived, raw payload);
        the actual compare-and-swap compression is delegated to
        archive_job_log (T1), which rechecks the full eligibility incl. the
        diagnostic guard and keeps compression outside the write lock.
        """
        cutoff_text = str(cutoff.isoformat() if hasattr(cutoff, "isoformat") else cutoff)
        with self._lock:
            connection = self._connect()
            try:
                excluded = list(dict.fromkeys(exclude_ids or ()))
                ids = self._selected_candidate_ids_locked(
                    connection,
                    with_cutoff=True,
                    cutoff_text=cutoff_text,
                    require_unarchived=True,
                    limit=max(1, limit) + len(excluded),
                    exclude_ids=excluded,
                )
                return ids[: max(0, limit)]
            finally:
                connection.close()

    def evict_job_logs_for_quota(
        self,
        limit: int = 25,
        exclude_ids: Optional[set] = None,
        *,
        free_to_bytes: int,
    ) -> Dict[str, Any]:
        """Evict the oldest finished eligible logs regardless of age (S3, B2).

        STRICT byte-budget eviction used only by the quota policy: the byte
        budget is REQUIRED, the compact total is measured inside this same
        BEGIN IMMEDIATE transaction (metadata only), and eviction only starts
        when measured > budget (a total exactly AT the quota never deletes).
        After every successful deletion the total is re-measured and the loop
        stops the moment measured <= budget — a batch is never deleted as a
        whole when the excess is smaller than one row.

        The stored Global Settings quota is re-read inside the same
        transaction (same database) and overrides the caller's possibly stale
        threshold. If a settings row exists and its quota is cleared or
        maintenance is disabled, no eviction is allowed. Without a settings
        row, direct repository callers supply the byte budget. The compact
        selection and policy index keep
        the wide jobs rows untouched during selection; the caller keeps a
        per-candidate cool-down (including `skipped` ids) so a row whose
        removal predicate or recheck failed is never retried every tick or
        starving other candidates. The batch bound (`limit` rows) still caps
        the transaction's per-row touching.
        """
        if (
            isinstance(free_to_bytes, bool)
            or not isinstance(free_to_bytes, int)
            or free_to_bytes < 0
        ):
            raise ValueError("free_to_bytes must be a non-negative integer byte budget")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                measured = self._measured_locked(connection)
                row = connection.execute(
                    "SELECT * FROM settings WHERE id = 'default'"
                ).fetchone()
                stored_gib = (
                    row["job_log_quota_gib"]
                    if row is not None and "job_log_quota_gib" in row.keys()
                    else None
                )
                stored_enabled = (
                    row["job_log_maintenance_enabled"]
                    if row is not None and "job_log_maintenance_enabled" in row.keys()
                    else 0
                )
                # T3 S5: the transactional recheck uses the effective quota
                # flag; an explicit stored decision wins, and only the legacy
                # default falls back to `maintenance_enabled AND configured`.
                stored_flag = (
                    row["job_log_quota_enabled"]
                    if row is not None and "job_log_quota_enabled" in row.keys()
                    else None
                )
                configured_gib = type(stored_gib) is int and stored_gib > 0
                if stored_flag is not None:
                    quota_active = bool(stored_flag) and configured_gib
                else:
                    quota_active = stored_enabled == 1 and configured_gib
                if row is None:
                    budget = free_to_bytes
                elif quota_active:
                    budget = stored_gib * JOB_LOG_QUOTA_GIB_TO_BYTES
                else:
                    budget = None
                evicted: List[str] = []
                skipped: List[str] = []
                freed_total = 0
                if budget is not None and measured > budget:
                    ids = self._selected_candidate_ids_locked(
                        connection,
                        with_cutoff=False,
                        limit=max(1, limit),
                        exclude_ids=list(exclude_ids or ()),
                    )
                    for job_id in ids:
                        outcome, freed = self._policy_removal_locked(connection, job_id)
                        if outcome == "removed":
                            evicted.append(job_id)
                            freed_total += freed
                            measured = self._measured_locked(connection)
                            if measured <= budget:
                                break
                        else:
                            skipped.append(job_id)
                connection.commit()
                return {
                    "evicted": len(evicted),
                    "bytes_freed": freed_total,
                    "jobs": evicted,
                    "skipped": skipped,
                    "budget_bytes": budget,
                    "measured_bytes": measured,
                }
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def job_log_policy_events(self) -> List[Dict[str, Any]]:
        """Bounded recent policy events (quota breaches and removals)."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = ?", (JOB_LOG_POLICY_EVENTS_META_KEY,)
            ).fetchone()
        try:
            events = _json_load(row["value"], []) if row else []
        except (TypeError, ValueError, json.JSONDecodeError):
            events = []
        return events if isinstance(events, list) else []

    @staticmethod
    def _append_policy_events_locked(connection, entries: List[Dict[str, Any]]) -> None:
        """Append bounded policy events inside an open immediate transaction."""
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = ?",
            (JOB_LOG_POLICY_EVENTS_META_KEY,),
        ).fetchone()
        try:
            events = _json_load(row["value"], []) if row else []
        except (TypeError, ValueError, json.JSONDecodeError):
            events = []
        if not isinstance(events, list):
            events = []
        events.extend(entries)
        events = events[-JOB_LOG_POLICY_EVENT_LIMIT:]
        connection.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            (JOB_LOG_POLICY_EVENTS_META_KEY, json.dumps(events)),
        )

    def record_job_log_policy_events(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Append bounded policy events, keeping the most recent only."""
        if not entries:
            return self.job_log_policy_events()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._append_policy_events_locked(connection, entries)
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = ?",
                    (JOB_LOG_POLICY_EVENTS_META_KEY,),
                ).fetchone()
                try:
                    events = _json_load(row["value"], []) if row else []
                except (TypeError, ValueError, json.JSONDecodeError):
                    events = []
                connection.commit()
                return events if isinstance(events, list) else []
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    # ------------------------------------------------------------------
    # T3 S4: confirmed admin cutoff prune, persisted and resumable.
    # The run state lives in schema_meta so it survives restarts; batches use
    # a durable ordered (finished_at, job_id) cursor over the compact policy
    # index, one short BEGIN IMMEDIATE batch at a time, never a full scan.
    # The cursor advances past protected synthetic rows too, so no cooldown
    # can stall completion.
    # ------------------------------------------------------------------

    @staticmethod
    def _prune_state_locked(connection) -> Optional[Dict[str, Any]]:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (JOB_LOG_PRUNE_STATE_META_KEY,)
        ).fetchone()
        if not row:
            return None
        try:
            state = _json_load(row["value"], None)
        except (TypeError, ValueError, json.JSONDecodeError):
            state = None
        return state if isinstance(state, dict) else None

    def job_log_prune_state(self) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            return self._prune_state_locked(connection)

    @staticmethod
    def _validate_prune_cutoff_date(cutoff_date: str) -> str:
        if isinstance(cutoff_date, bool) or not isinstance(cutoff_date, str):
            raise ValueError("cutoff_date must be a YYYY-MM-DD calendar date string")
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", cutoff_date):
            raise ValueError("cutoff_date must be a YYYY-MM-DD calendar date string")
        try:
            datetime.strptime(cutoff_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(
                f"cutoff_date is not a real calendar date: {cutoff_date}"
            ) from exc
        return cutoff_date

    def request_job_log_prune(self, cutoff_date: str, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Accept one confirmed cutoff prune as persisted pending run state.

        Idempotent for the same cutoff, a conflicting pending prune raises
        JobLogPruneConflict. The cutoff is the exclusive next midnight (UTC):
        log bodies of jobs finished before or on the selected date are pruned.
        The request itself deletes nothing and returns bounded state without
        scanning or fabricating historical counts.
        """
        validated = self._validate_prune_cutoff_date(cutoff_date)
        year, month, day = (int(part) for part in validated.split("-"))
        next_midnight = datetime(year, month, day) + timedelta(days=1)
        now_text = (now or utcnow()).isoformat()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = self._prune_state_locked(connection)
                if existing and existing.get("status") == "pending":
                    if existing.get("cutoff_date") == validated:
                        connection.commit()
                        return {**existing, "created": False}
                    connection.rollback()
                    raise JobLogPruneConflict(
                        "a pending job log prune with cutoff "
                        f"{existing.get('cutoff_date')} is already in progress"
                    )
                state = {
                    "cutoff_date": validated,
                    "cutoff": next_midnight.isoformat(),
                    "status": "pending",
                    "processed": 0,
                    "bytes_freed": 0,
                    "cursor_finished_at": "",
                    "cursor_job_id": "",
                    "batch_size": DEFAULT_JOB_LOG_PRUNE_BATCH_SIZE,
                    "requested_at": now_text,
                    "updated_at": now_text,
                }
                connection.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                    (JOB_LOG_PRUNE_STATE_META_KEY, json.dumps(state)),
                )
                connection.commit()
                return {**state, "created": True}
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @staticmethod
    def _prune_candidates_locked(
        connection,
        *,
        cutoff_text: str,
        cursor_finished_at: str,
        cursor_job_id: str,
        limit: int,
    ) -> List[Tuple[str, str]]:
        """Covering oldest-first (finished_at, job_id) prune selection.

        A1: strict `finished_at < :cutoff` against the exclusive next-midnight
        cutoff, so exact-midnight stored forms and the `+00:00`/microsecond
        SQLite ISO variants are consistently excluded in both the compact
        selection and the wide-row safety check.
        """
        rows = connection.execute(
            f"""
            SELECT job_id, finished_at FROM {JOB_LOG_SIZES_TABLE}
            WHERE payload_present = 1 AND terminal = 1 AND removed = 0
              AND finished_at IS NOT NULL AND finished_at < :cutoff
              AND (finished_at > :after_finished OR (finished_at = :after_finished AND job_id > :after_job_id))
            ORDER BY finished_at ASC, job_id ASC
            LIMIT :limit
            """,
            {
                "cutoff": cutoff_text,
                "after_finished": cursor_finished_at,
                "after_job_id": cursor_job_id,
                "limit": max(1, limit),
            },
        ).fetchall()
        return [(row["job_id"], row["finished_at"]) for row in rows]

    def run_job_log_prune_batch(self, batch_size: Optional[int] = None, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Advance the persisted prune cursor by one bounded BEGIN IMMEDIATE batch.

        With no pending state this is a no-op (`active` False). B1: the cursor
        is not read or advanced until the bounded compact backfill completion
        marker exists — the id-keyset backfill rows surface out of finish-time
        order, so advancing a finish-time cursor over partial metadata could
        silently skip later-discovered older rows. Completion is then written
        when the follow-up probe finds no further matching rows past the
        cursor, and the prune never reports a premature completion over the
        cooldown of a protected synthetic job. Bounded note: each batch
        touches at most `batch_size` selected rows (one recheck + one
        predicate write each).
        """
        now_text = (now or utcnow()).isoformat()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                state = self._prune_state_locked(connection)
                if not state or state.get("status") != "pending":
                    connection.commit()
                    return {
                        "active": False,
                        "complete": bool(state),
                        "wait_for_indexing": False,
                        "removed": [],
                        "bytes_freed": 0,
                        "skipped": [],
                        "state": state,
                    }
                marker = connection.execute(
                    "SELECT 1 FROM schema_meta WHERE key = ?", (JOB_LOG_SIZES_BACKFILL_META_KEY,)
                ).fetchone()
                if marker is None:
                    # B1: wait for the bounded backfill to complete BEFORE
                    # reading or advancing the finish-time cursor; nothing is
                    # deleted and the state stays exactly as requested.
                    connection.commit()
                    return {
                        "active": True,
                        "complete": False,
                        "wait_for_indexing": True,
                        "removed": [],
                        "bytes_freed": 0,
                        "skipped": [],
                        "state": state,
                    }
                limit = DEFAULT_JOB_LOG_PRUNE_BATCH_SIZE
                if isinstance(batch_size, int) and not isinstance(batch_size, bool) and 0 < batch_size <= MAX_PRUNE_BATCH_ROWS:
                    limit = batch_size
                cutoff_text = str(state["cutoff"])
                candidates = self._prune_candidates_locked(
                    connection,
                    cutoff_text=cutoff_text,
                    cursor_finished_at=str(state.get("cursor_finished_at") or ""),
                    cursor_job_id=str(state.get("cursor_job_id") or ""),
                    limit=limit,
                )
                removed: List[str] = []
                skipped: List[str] = []
                freed_total = 0
                last_finished_at = str(state.get("cursor_finished_at") or "")
                last_job_id = str(state.get("cursor_job_id") or "")
                for job_id, finished_at in candidates:
                    outcome, freed = self._policy_removal_locked(
                        connection,
                        job_id,
                        cutoff_text=cutoff_text,
                        # A1: the prune cut is strict before the next midnight.
                        cutoff_inclusive=False,
                    )
                    if outcome == "removed":
                        removed.append(job_id)
                        freed_total += freed
                    else:
                        skipped.append(job_id)
                    last_finished_at, last_job_id = str(finished_at), str(job_id)
                if candidates:
                    followup = self._prune_candidates_locked(
                        connection,
                        cutoff_text=cutoff_text,
                        cursor_finished_at=last_finished_at,
                        cursor_job_id=last_job_id,
                        limit=1,
                    )
                    probe_empty = not followup
                else:
                    probe_empty = True
                # The backfill completion marker was verified above, so the
                # compact metadata here is complete by definition.
                complete = probe_empty
                next_state = dict(state)
                next_state.update({
                    "processed": int(state.get("processed") or 0) + len(candidates),
                    "bytes_freed": int(state.get("bytes_freed") or 0) + freed_total,
                    "cursor_finished_at": last_finished_at if candidates else state.get("cursor_finished_at"),
                    "cursor_job_id": last_job_id if candidates else state.get("cursor_job_id"),
                    "updated_at": now_text,
                })
                if complete:
                    next_state["status"] = "complete"
                    next_state["completed_at"] = now_text
                if removed:
                    self._append_policy_events_locked(connection, [{
                        "at": now_text,
                        "kind": "logs_pruned",
                        "cutoff_date": state["cutoff_date"],
                        "job_count": len(removed),
                        "bytes_freed": freed_total,
                        "job_ids": removed[:5],
                    }])
                connection.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                    (JOB_LOG_PRUNE_STATE_META_KEY, json.dumps(next_state)),
                )
                connection.commit()
                return {
                    "active": True,
                    "complete": next_state["status"] == "complete",
                    "wait_for_indexing": False,
                    "removed": removed,
                    "bytes_freed": freed_total,
                    "skipped": skipped,
                    "state": next_state,
                }
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def has_active_backup_for_target(self, target_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM jobs WHERE target_id = ? AND command = ? "
                "AND status IN (?, ?) LIMIT 1",
                (target_id, "backup.run", JobStatus.PENDING, JobStatus.IN_PROGRESS),
            ).fetchone()
        return row is not None

    def has_blocking_backup_for_schedule_handover(self, target_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM jobs WHERE target_id = ? AND command = ? "
                "AND status IN (?, ?, ?) LIMIT 1",
                (target_id, "backup.run", JobStatus.PENDING, JobStatus.IN_PROGRESS, JobStatus.UNCONFIRMED),
            ).fetchone()
        return row is not None

    def find_active_snapshot_sync(self, target_id: str, worker_id: str) -> Optional[JobRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE target_id = ? AND worker_id = ? AND command = ? "
                "AND status IN (?, ?) ORDER BY submitted_at DESC LIMIT 1",
                (target_id, worker_id, "snapshots.list", JobStatus.PENDING, JobStatus.IN_PROGRESS),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def active_for_worker(self, worker_id: str) -> List[Tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, status FROM jobs WHERE status IN (?, ?) "
                "AND (worker_id = ? OR owner_worker_id = ?) ORDER BY submitted_at DESC",
                (JobStatus.PENDING, JobStatus.IN_PROGRESS, worker_id, worker_id),
            ).fetchall()
        return [(row["id"], row["status"]) for row in rows]

    def list_for_lease_context(self) -> List[JobRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, worker_id, command, target_id, status, owner_worker_id,
                       lease_issued_at, lease_expires_at, attempt_count,
                       result_summary_json, submitted_at, started_at, finished_at, updated_at
                FROM jobs
                WHERE status = ? AND lease_expires_at IS NOT NULL
                """,
                (JobStatus.IN_PROGRESS,),
            ).fetchall()
        jobs = []
        for row in rows:
            try:
                result_summary = _json_load(row["result_summary_json"], None)
            except (TypeError, ValueError, json.JSONDecodeError):
                result_summary = None
            jobs.append(
                JobRecord(
                    id=row["id"],
                    worker_id=row["worker_id"],
                    command=row["command"],
                    target_id=row["target_id"],
                    status=JobStatus.normalize(row["status"]),
                    owner_worker_id=row["owner_worker_id"],
                    lease_issued_at=_dt(row["lease_issued_at"]),
                    lease_expires_at=_dt(row["lease_expires_at"]),
                    attempt_count=row["attempt_count"] or 0,
                    result_summary=result_summary if isinstance(result_summary, dict) else None,
                    submitted_at=_dt(row["submitted_at"]) or datetime.utcnow(),
                    started_at=_dt(row["started_at"]),
                    finished_at=_dt(row["finished_at"]),
                    updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
                )
            )
        return jobs

    def list_for_unconfirmed_outcomes(self) -> List[JobRecord]:
        """Bounded candidate query for the 24-hour outcome reconciliation.

        Mirrors list_for_lease_context: only the lightweight columns needed
        for the reconciliation are decoded (no payload/log deserialization)
        and only IN_PROGRESS jobs flagged with the expired-lease diagnostic
        are returned.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, worker_id, command, target_id, status, owner_worker_id,
                       lease_issued_at, lease_expires_at, attempt_count,
                       result_summary_json, submitted_at, started_at, finished_at, updated_at
                FROM jobs
                WHERE status = ? AND json_extract(result_summary_json, '$.diagnostic_code') = ?
                """,
                (JobStatus.IN_PROGRESS, LEASE_DIAGNOSTIC_CODE),
            ).fetchall()
        jobs = []
        for row in rows:
            try:
                result_summary = _json_load(row["result_summary_json"], None)
            except (TypeError, ValueError, json.JSONDecodeError):
                result_summary = None
            jobs.append(
                JobRecord(
                    id=row["id"],
                    worker_id=row["worker_id"],
                    command=row["command"],
                    target_id=row["target_id"],
                    status=JobStatus.normalize(row["status"]),
                    owner_worker_id=row["owner_worker_id"],
                    lease_issued_at=_dt(row["lease_issued_at"]),
                    lease_expires_at=_dt(row["lease_expires_at"]),
                    attempt_count=row["attempt_count"] or 0,
                    result_summary=result_summary if isinstance(result_summary, dict) else None,
                    submitted_at=_dt(row["submitted_at"]) or datetime.utcnow(),
                    started_at=_dt(row["started_at"]),
                    finished_at=_dt(row["finished_at"]),
                    updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
                )
            )
        return jobs

    def update_lease_context(self, job_id: str, lease_context: Dict[str, Any]) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT result_summary_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return False
            try:
                summary = _json_load(row["result_summary_json"], None)
            except (TypeError, ValueError, json.JSONDecodeError):
                summary = None
            summary = dict(summary) if isinstance(summary, dict) else {}
            summary["lease_context"] = dict(lease_context)
            connection.execute(
                "UPDATE jobs SET result_summary_json = ? WHERE id = ? AND status = ?",
                (json.dumps(summary), job_id, JobStatus.IN_PROGRESS),
            )
            return True

    @staticmethod
    def _listing_where(filters: Optional[Dict[str, Any]]) -> Tuple[str, List[Any]]:
        clauses: List[str] = []
        parameters: List[Any] = []
        active = {key: value for key, value in (filters or {}).items() if value}
        for key in ("status", "command", "trigger", "target_id"):
            if key in active:
                clauses.append(f"{key} = ?")
                parameters.append(active[key])
        if "identity_search" in active:
            escaped = str(active["identity_search"]).lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(
                "(lower(target_id) LIKE ? ESCAPE '\\' OR lower(worker_id) LIKE ? ESCAPE '\\' "
                "OR target_id IN (SELECT value FROM json_each(?)) "
                "OR worker_id IN (SELECT value FROM json_each(?)))"
            )
            parameters.extend((f"%{escaped}%", f"%{escaped}%", json.dumps(active.get("identity_target_ids", [])), json.dumps(active.get("identity_worker_ids", []))))
        if "id_prefix" in active:
            escaped = str(active["id_prefix"]).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("id LIKE ? ESCAPE '\\'")
            parameters.append(f"{escaped}%")
        if "started_from" in active:
            clauses.append("started_at >= ?")
            parameters.append(active["started_from"].isoformat())
        if "finished_to" in active:
            clauses.append("finished_at <= ?")
            parameters.append(active["finished_to"].isoformat())
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", parameters

    def list_for_listing(
        self,
        limit: Optional[int] = None,
        offset: int = 0,
        filters: Optional[Dict[str, Any]] = None,
        sort: str = "submitted_at",
        order: str = "desc",
    ) -> Tuple[List[JobRecord], int]:
        if sort not in JOB_LISTING_SORT_KEYS:
            raise ValueError(f"unsupported sort key: {sort}")
        if order not in ("asc", "desc"):
            raise ValueError(f"unsupported sort order: {order}")
        start = max(0, offset)
        columns = """
            id, worker_id, command, requested_by, target_id, trigger, status,
            attempt_count, submitted_at, started_at, finished_at, updated_at
        """
        where, parameters = self._listing_where(filters)
        query = f"SELECT {columns} FROM jobs{where} ORDER BY {sort} {order.upper()}, id DESC"
        page_parameters: List[Any] = list(parameters)
        if limit is not None and limit > 0:
            query += " LIMIT ? OFFSET ?"
            page_parameters.extend((limit, start))
        elif start > 0:
            query += " LIMIT -1 OFFSET ?"
            page_parameters.append(start)
        with self._connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) AS total FROM jobs{where}", parameters).fetchone()["total"]
            rows = connection.execute(query, page_parameters).fetchall()
        return [self._row_to_listing_job(row) for row in rows], total

    def list_started_between(self, command: str, start: datetime, end: datetime) -> List[JobRecord]:
        start_utc = start.astimezone(timezone.utc).replace(tzinfo=None) if start.tzinfo else start
        end_utc = end.astimezone(timezone.utc).replace(tzinfo=None) if end.tzinfo else end
        if end_utc <= start_utc:
            return []
        # The audit report aggregates per-job result_summary metrics (T5 S2), so
        # the date-bounded report query selects the existing result_summary_json
        # column (no schema migration) and hydrates it onto the record. The public
        # list_for_listing query stays lightweight; only this report query returns
        # the result-summary mapping, in a single bounded query (no N+1 reads).
        columns = """
            id, worker_id, command, requested_by, target_id, trigger, status,
            attempt_count, submitted_at, started_at, finished_at, updated_at,
            result_summary_json
        """
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {columns} FROM jobs WHERE command = ? AND started_at >= ? AND started_at < ? "
                "ORDER BY started_at ASC, id ASC",
                (command, start_utc.isoformat(), end_utc.isoformat()),
            ).fetchall()
        jobs: List[JobRecord] = []
        for row in rows:
            job = self._row_to_listing_job(row)
            job.result_summary = _json_load(row["result_summary_json"], None)
            jobs.append(job)
        return jobs

    def job_stats(self, tz_offset_minutes: int = 0, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Aggregate dashboard stats from the compact projection table.

        Same day keys, tz-offset grouping, window semantics, status
        normalization and result shape as the historical full-table scan, but
        it only reads the compact facts (job_id, normalized status,
        started_at) instead of probing the wide jobs rows.
        """
        day_keys = job_stats_day_keys(now or datetime.utcnow(), tz_offset_minutes)
        window_start = datetime.fromisoformat(day_keys[0]) - timedelta(minutes=tz_offset_minutes)
        with self._connect() as connection:
            status_rows = connection.execute(
                f"SELECT status, COUNT(*) AS total FROM {JOB_STATS_FACTS_TABLE} GROUP BY status"
            ).fetchall()
            day_rows = connection.execute(
                f"""
                SELECT date(started_at, ?) AS day, status, COUNT(*) AS total
                FROM {JOB_STATS_FACTS_TABLE}
                WHERE started_at IS NOT NULL AND started_at >= ?
                GROUP BY day, status
                """,
                (f"{tz_offset_minutes:+d} minutes", window_start.isoformat()),
            ).fetchall()
        status_counts: Dict[str, int] = {}
        for row in status_rows:
            status = JobStatus.normalize(row["status"])
            status_counts[status] = status_counts.get(status, 0) + row["total"]
        day_status_counts: Dict[Tuple[str, str], int] = {}
        for row in day_rows:
            key = (row["day"], JobStatus.normalize(row["status"]))
            day_status_counts[key] = day_status_counts.get(key, 0) + row["total"]
        return build_job_stats(sum(status_counts.values()), status_counts, day_status_counts, day_keys)

    @staticmethod
    def _row_to_listing_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            worker_id=row["worker_id"],
            command=row["command"],
            requested_by=row["requested_by"],
            target_id=row["target_id"],
            trigger=row["trigger"],
            status=JobStatus.normalize(row["status"]),
            attempt_count=row["attempt_count"] or 0,
            submitted_at=_dt(row["submitted_at"]) or datetime.utcnow(),
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )

    @staticmethod
    def _reconcile_expired_leases_locked(connection, now) -> set:
        """Apply the awaiting-confirmation diagnostic to every expired lease.

        Returns the identifiers of the jobs transitioned by this phase; the
        caller combines them with the unknown-outcome phase so the public
        count stays "unique jobs changed by reconcile" (never two transitions
        for the same job in one pass).
        """
        rows = connection.execute(
            """
            SELECT id, worker_id, command, target_id, owner_worker_id,
                   result_summary_json, log_lines_json, log_timestamps_json, lease_issued_at,
                   lease_expires_at, updated_at
            FROM jobs
            WHERE status = ? AND lease_expires_at IS NOT NULL
            """,
            (JobStatus.IN_PROGRESS,),
        ).fetchall()
        interruption_log = "Worker lease expired before terminal status was reported."
        reconciled: set = set()
        for row in rows:
            lease_expires_at = _dt(row["lease_expires_at"])
            if lease_expires_at and lease_expires_at.tzinfo:
                lease_expires_at = lease_expires_at.astimezone(timezone.utc).replace(tzinfo=None)
            if not lease_expires_at or lease_expires_at > now:
                continue
            result_summary = _json_load(row["result_summary_json"], None)
            result_summary = dict(result_summary) if isinstance(result_summary, dict) else {}
            result_summary["recovery"] = "awaiting_worker_confirmation"
            result_summary["diagnostic_code"] = LEASE_DIAGNOSTIC_CODE
            result_summary["diagnostic_category"] = LEASE_DIAGNOSTIC_CATEGORY
            result_summary["diagnostic_message"] = LEASE_DIAGNOSTIC_MESSAGE
            result_summary[CONFIRMATION_DEADLINE_KEY] = _datetime_text(
                lease_expires_at + timedelta(seconds=JOB_UNCONFIRMED_AFTER_SECONDS)
            )
            result_summary["lease_diagnostics"] = _lease_diagnostics(
                job_id=row["id"],
                worker_id=row["owner_worker_id"] or row["worker_id"],
                target_id=row["target_id"],
                command=row["command"],
                lease_issued_at=_dt(row["lease_issued_at"]),
                lease_expires_at=lease_expires_at,
                updated_at=_dt(row["updated_at"]),
                result_summary=result_summary,
                now=now,
            )
            result_summary.pop("lease_context", None)
            log_lines = _json_load(row["log_lines_json"], [])
            log_lines = list(log_lines) if isinstance(log_lines, list) else []
            log_timestamps = _json_load(row["log_timestamps_json"], None)
            if not isinstance(log_timestamps, list) or len(log_timestamps) != len(log_lines):
                log_timestamps = [None] * len(log_lines)
            if interruption_log not in log_lines:
                log_lines.append(interruption_log)
                log_timestamps.append(None)
            # Keep owner_worker_id and lease_token: they are the proof a late
            # terminal report from the owning worker must present to replace
            # this synthetic failure with the real result.
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = ?, lease_issued_at = NULL, lease_expires_at = NULL,
                    result_summary_json = ?, log_lines_json = ?, log_timestamps_json = ?,
                    finished_at = NULL, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.IN_PROGRESS,
                    json.dumps(result_summary),
                    json.dumps(log_lines),
                    json.dumps(log_timestamps) if any(log_timestamps) else None,
                    now.isoformat(),
                    row["id"],
                    JobStatus.IN_PROGRESS,
                ),
            )
            reconciled.add(row["id"])
        return reconciled

    @staticmethod
    def _record_unknown_outcome_locked(connection, now) -> set:
        """S7: flip awaiting-confirmation jobs whose deadline passed to unknown.

        No terminal result ever arrived, so the outcome is neither failed nor
        succeeded: the outcome is unknown. Ownership and lease token stay so
        the authenticated owner can still deliver a late terminal report; the
        job stops counting as an active execution for new-task guards.

        Returns the identifiers of the jobs transitioned by this phase.
        """
        unknown_log = UNCONFIRMED_OUTCOME_LOG
        rows = connection.execute(
            """
            SELECT id, command, result_summary_json, log_lines_json, updated_at
            FROM jobs
            WHERE status = ?
            """,
            (JobStatus.IN_PROGRESS,),
        ).fetchall()
        recorded: set = set()
        for row in rows:
            try:
                summary = _json_load(row["result_summary_json"], None)
            except (TypeError, ValueError, json.JSONDecodeError):
                summary = None
            if not isinstance(summary, dict):
                continue  # Malformed summary: keep the job blocked (fail closed).
            if summary.get("diagnostic_code") != LEASE_DIAGNOSTIC_CODE:
                continue
            deadline = _confirmation_deadline(summary, _dt(row["updated_at"]))
            read_only = row["command"] in READ_ONLY_SNAPSHOT_COMMANDS
            if not read_only and (deadline is None or deadline > now):
                continue
            unknown_log = READ_ONLY_UNCONFIRMED_LOG if read_only else UNCONFIRMED_OUTCOME_LOG
            summary["recovery"] = UNCONFIRMED_OUTCOME_RECOVERY
            log_lines = _json_load(row["log_lines_json"], [])
            log_lines = list(log_lines) if isinstance(log_lines, list) else []
            if unknown_log not in log_lines:
                log_lines.append(unknown_log)
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = ?, result_summary_json = ?, log_lines_json = ?,
                    finished_at = NULL, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.UNCONFIRMED,
                    json.dumps(summary),
                    json.dumps(log_lines),
                    now.isoformat(),
                    row["id"],
                    JobStatus.IN_PROGRESS,
                ),
            )
            recorded.add(row["id"])
        return recorded

    def reconcile_expired_leases(self) -> int:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = utcnow()
                expired_ids = self._reconcile_expired_leases_locked(connection, now)
                unknown_ids = self._record_unknown_outcome_locked(connection, utcnow())
                # Previous contract: the return counts unique reconciled jobs.
                # A job that both expires and passes its 24-hour deadline in
                # the same pass is still one reconciled job.
                reconciled = len(expired_ids | unknown_ids)
                connection.commit()
                return reconciled
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

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
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = utcnow()
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                lease_expires_at = _dt(row["lease_expires_at"]) if row else None
                if lease_expires_at and lease_expires_at.tzinfo:
                    lease_expires_at = lease_expires_at.astimezone(timezone.utc).replace(tzinfo=None)
                if (
                    row is None
                    or JobStatus.normalize(row["status"]) != JobStatus.IN_PROGRESS
                    or row["owner_worker_id"] != worker_id
                    or row["lease_token"] != lease_token
                ):
                    connection.commit()
                    return None
                expires_at = now + timedelta(seconds=lease_duration_seconds)
                current_summary = _json_load(row["result_summary_json"], None)
                current_summary = dict(current_summary) if isinstance(current_summary, dict) else {}
                if current_summary.get("diagnostic_code") == LEASE_DIAGNOSTIC_CODE:
                    for key in _renewal_cleared_summary_keys():
                        current_summary.pop(key, None)
                context = _lease_context_for_values(
                    job_id=row["id"],
                    worker_id=row["owner_worker_id"] or row["worker_id"],
                    target_id=row["target_id"],
                    command=row["command"],
                    lease_issued_at=_dt(row["lease_issued_at"]),
                    lease_expires_at=expires_at,
                    updated_at=now,
                    result_summary=current_summary,
                    now=now,
                )
                context["lease_expires_at"] = _datetime_text(expires_at)
                context["last_repository_update_at"] = _datetime_text(now)
                current_summary["lease_context"] = context
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET lease_expires_at = ?, result_summary_json = ?, updated_at = ?
                    WHERE id = ? AND status = ? AND owner_worker_id = ?
                      AND lease_token = ?
                    """,
                    (
                        expires_at.isoformat(),
                        json.dumps(current_summary),
                        now.isoformat(),
                        job_id,
                        JobStatus.IN_PROGRESS,
                        worker_id,
                        lease_token,
                    ),
                )
                if updated.rowcount != 1:
                    connection.commit()
                    return None
                renewed = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                connection.commit()
                return self._row_to_job(renewed) if renewed else None
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

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
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = utcnow()
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                lease_expires_at = _dt(row["lease_expires_at"]) if row else None
                if lease_expires_at and lease_expires_at.tzinfo:
                    lease_expires_at = lease_expires_at.astimezone(timezone.utc).replace(tzinfo=None)
                if (
                    row is None
                    or JobStatus.normalize(row["status"]) != JobStatus.IN_PROGRESS
                    or row["owner_worker_id"] != worker_id
                    or not hmac.compare_digest(row["lease_token"] or "", lease_token or "")
                ):
                    connection.commit()
                    return None

                current_summary = _json_load(row["result_summary_json"], None)
                current_summary = dict(current_summary) if isinstance(current_summary, dict) else {}
                try:
                    current_sequence = int(current_summary.get("progress_sequence", 0) or 0)
                except (TypeError, ValueError):
                    current_sequence = 0
                if sequence <= current_sequence:
                    connection.commit()
                    return self._row_to_job(row)

                if isinstance(result_summary, dict):
                    current_summary.update(result_summary)
                current_summary["progress"] = dict(progress)
                current_summary["progress_sequence"] = sequence
                # S7 duplicate-execution guard: while the owner keeps showing
                # liveness after lease expiry, the outcome stays awaiting
                # confirmation instead of flipping to unknown, so new tasks
                # remain blocked for a demonstrably still-running job.
                if current_summary.get("diagnostic_code") == LEASE_DIAGNOSTIC_CODE:
                    current_summary[CONFIRMATION_DEADLINE_KEY] = _datetime_text(
                        now + timedelta(seconds=JOB_UNCONFIRMED_AFTER_SECONDS)
                    )
                context = _lease_context_for_values(
                    job_id=row["id"],
                    worker_id=row["owner_worker_id"] or row["worker_id"],
                    target_id=row["target_id"],
                    command=row["command"],
                    lease_issued_at=_dt(row["lease_issued_at"]),
                    lease_expires_at=lease_expires_at,
                    updated_at=now,
                    result_summary=current_summary,
                    now=now,
                )
                context["last_repository_update_at"] = _datetime_text(now)
                context["last_progress_at"] = _datetime_text(now)
                context["last_progress_sequence"] = sequence
                current_summary["lease_context"] = context
                current_logs = _json_load(row["log_lines_json"], [])
                current_logs = list(current_logs) if isinstance(current_logs, list) else []
                current_timestamps = _json_load(row["log_timestamps_json"], None)
                if not isinstance(current_timestamps, list) or len(current_timestamps) != len(current_logs):
                    current_timestamps = [None] * len(current_logs)
                incoming_lines = list(log_lines or [])
                incoming_timestamps = (
                    list(log_timestamps)
                    if isinstance(log_timestamps, list) and len(log_timestamps) == len(incoming_lines)
                    else [None] * len(incoming_lines)
                )
                combined_logs = current_logs + incoming_lines
                combined_timestamps = current_timestamps + incoming_timestamps
                offset = max(0, len(combined_logs) - self.MAX_LOG_LINES)
                current_logs = combined_logs[offset:]
                current_timestamps = combined_timestamps[offset:]
                while current_logs and sum(len(line) for line in current_logs if isinstance(line, str)) > self.MAX_LOG_CHARS:
                    current_logs.pop(0)
                    current_timestamps.pop(0)
                updated = connection.execute(
                    """
                    UPDATE jobs
                    SET result_summary_json = ?, log_lines_json = ?, log_timestamps_json = ?, updated_at = ?
                    WHERE id = ? AND status = ? AND owner_worker_id = ?
                      AND lease_token = ?
                    """,
                    (
                        json.dumps(current_summary),
                        json.dumps(current_logs),
                        json.dumps(current_timestamps) if any(current_timestamps) else None,
                        now.isoformat(),
                        job_id,
                        JobStatus.IN_PROGRESS,
                        worker_id,
                        lease_token,
                    ),
                )
                if updated.rowcount != 1:
                    connection.commit()
                    return None
                saved = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                connection.commit()
                return self._row_to_job(saved) if saved else None
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def claim_pending_for_worker(
        self,
        worker_id: str,
        lease_duration_seconds: int = 300,
        priority_commands: Optional[Tuple[str, ...]] = None,
    ) -> List[JobRecord]:
        if lease_duration_seconds <= 0:
            raise ValueError("lease duration must be positive")
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = utcnow()
                self._reconcile_expired_leases_locked(connection, now)
                preferred_commands = tuple(dict.fromkeys(priority_commands or ()))
                if preferred_commands:
                    placeholders = ", ".join("?" for _ in preferred_commands)
                    query = f"""
                        SELECT * FROM jobs
                        WHERE worker_id = ? AND status = ?
                        ORDER BY CASE WHEN command IN ({placeholders}) THEN 0 ELSE 1 END,
                                 submitted_at ASC
                        LIMIT 1
                    """
                    parameters = (worker_id, JobStatus.PENDING, *preferred_commands)
                else:
                    query = """
                        SELECT * FROM jobs
                        WHERE worker_id = ? AND status = ?
                        ORDER BY submitted_at ASC
                        LIMIT 1
                    """
                    parameters = (worker_id, JobStatus.PENDING)
                rows = connection.execute(query, parameters).fetchall()
                expires_at = now + timedelta(seconds=lease_duration_seconds)
                claimed = []
                for row in rows:
                    lease_token = secrets.token_urlsafe(32)
                    attempt_count = (row["attempt_count"] or 0) + 1
                    current_summary = _json_load(row["result_summary_json"], None)
                    current_summary = dict(current_summary) if isinstance(current_summary, dict) else {}
                    context = _lease_context_for_values(
                        job_id=row["id"],
                        worker_id=worker_id,
                        target_id=row["target_id"],
                        command=row["command"],
                        lease_issued_at=now,
                        lease_expires_at=expires_at,
                        updated_at=now,
                        result_summary=current_summary,
                        now=now,
                    )
                    context.update(
                        {
                            "lease_issued_at": _datetime_text(now),
                            "lease_expires_at": _datetime_text(expires_at),
                            "last_repository_update_at": _datetime_text(now),
                        }
                    )
                    current_summary["lease_context"] = context
                    updated = connection.execute(
                        """
                        UPDATE jobs
                        SET status = ?, owner_worker_id = ?, lease_token = ?,
                            lease_issued_at = ?, lease_expires_at = ?, attempt_count = ?,
                            started_at = ?, result_summary_json = ?, updated_at = ?
                        WHERE id = ? AND status = ?
                        """,
                        (
                            JobStatus.IN_PROGRESS,
                            worker_id,
                            lease_token,
                            now.isoformat(),
                            expires_at.isoformat(),
                            attempt_count,
                            now.isoformat(),
                            json.dumps(current_summary),
                            now.isoformat(),
                            row["id"],
                            JobStatus.PENDING,
                        ),
                    )
                    if updated.rowcount != 1:
                        continue
                    job = self._row_to_job(row)
                    job.status = JobStatus.IN_PROGRESS
                    job.owner_worker_id = worker_id
                    job.lease_token = lease_token
                    job.lease_issued_at = now
                    job.lease_expires_at = expires_at
                    job.attempt_count = attempt_count
                    job.started_at = now
                    job.updated_at = now
                    job.result_summary = current_summary
                    claimed.append(job)
                connection.commit()
                return claimed
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        keys = row.keys()
        archive_blob = row["job_log_brotli"] if "job_log_brotli" in keys else None
        log_lines_json = row["log_lines_json"]
        if (
            archive_blob is not None
            and (log_lines_json is None or log_lines_json in ("", JOB_LOG_ARCHIVE_SENTINEL))
        ):
            log_lines, log_timestamps = _decode_job_log_archive(archive_blob)
        else:
            log_lines = _json_load(log_lines_json, [])
            log_timestamps = _json_load(row["log_timestamps_json"], None)
        return JobRecord(
            id=row["id"],
            worker_id=row["worker_id"],
            command=row["command"],
            payload=_json_load(row["payload_json"], {}),
            requested_by=row["requested_by"],
            target_id=row["target_id"],
            trigger=row["trigger"],
            status=JobStatus.normalize(row["status"]),
            owner_worker_id=row["owner_worker_id"],
            lease_token=row["lease_token"],
            lease_issued_at=_dt(row["lease_issued_at"]),
            lease_expires_at=_dt(row["lease_expires_at"]),
            attempt_count=row["attempt_count"] or 0,
            result_summary=_json_load(row["result_summary_json"], None),
            log_lines=log_lines,
            log_timestamps=log_timestamps,
            submitted_at=_dt(row["submitted_at"]) or datetime.utcnow(),
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
            job_log_removed=bool(row["job_log_removed"]) if "job_log_removed" in keys else False,
        )


class SQLiteStorageProfileRepository(SQLiteRepositoryBase, StorageProfileRepository):
    def save(self, profile: StorageProfileRecord) -> StorageProfileRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO storage_profiles (
                    id, name, backend_type, environment_json, secret_refs_json, file_secret_refs_json,
                    runtime_volumes_json, labels_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile.id,
                    profile.name,
                    profile.backend_type,
                    json.dumps(profile.environment),
                    json.dumps(profile.secret_refs),
                    json.dumps(profile.file_secret_refs),
                    json.dumps(profile.runtime_volumes),
                    json.dumps(profile.labels),
                    profile.created_at.isoformat(),
                    profile.updated_at.isoformat(),
                ),
            )
        return profile

    def get(self, profile_id: str) -> Optional[StorageProfileRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM storage_profiles WHERE id = ?", (profile_id,)).fetchone()
        return self._row_to_profile(row) if row else None

    def list(self) -> List[StorageProfileRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM storage_profiles ORDER BY created_at ASC").fetchall()
        return [self._row_to_profile(row) for row in rows]

    def delete(self, profile_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM storage_profiles WHERE id = ?", (profile_id,))
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> StorageProfileRecord:
        return StorageProfileRecord(
            id=row["id"],
            name=row["name"],
            backend_type=row["backend_type"],
            environment=_json_load(row["environment_json"], {}),
            secret_refs=_json_load(row["secret_refs_json"], {}),
            file_secret_refs=_json_load(row["file_secret_refs_json"], {}),
            runtime_volumes=_json_load(row["runtime_volumes_json"], {}),
            labels=_json_load(row["labels_json"], {}),
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )


class SQLiteSecretRepository(SQLiteRepositoryBase, SecretRepository):
    def save(self, secret: SecretRecord) -> SecretRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO secrets (
                    id, name, scope, secret_type, ciphertext, key_version, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    secret.id,
                    secret.name,
                    secret.scope,
                    secret.secret_type,
                    secret.ciphertext,
                    secret.key_version,
                    json.dumps(secret.metadata),
                    secret.created_at.isoformat(),
                    secret.updated_at.isoformat(),
                ),
            )
        return secret

    def get(self, secret_id: str) -> Optional[SecretRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM secrets WHERE id = ?", (secret_id,)).fetchone()
        return self._row_to_secret(row) if row else None

    def list(self) -> List[SecretRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM secrets ORDER BY created_at ASC").fetchall()
        return [self._row_to_secret(row) for row in rows]

    def delete(self, secret_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM secrets WHERE id = ?", (secret_id,))

    @staticmethod
    def _row_to_secret(row: sqlite3.Row) -> SecretRecord:
        return SecretRecord(
            id=row["id"],
            name=row["name"],
            scope=row["scope"],
            secret_type=row["secret_type"],
            ciphertext=row["ciphertext"],
            key_version=row["key_version"],
            metadata=_json_load(row["metadata_json"], {}),
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )


class SQLiteNotificationChannelRepository(SQLiteRepositoryBase, NotificationChannelRepository):
    def claim_delivery_attempt(self, job_id: str, channel_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO notification_delivery_attempts (job_id, channel_id, claimed_at)
                VALUES (?, ?, ?)
                """,
                (job_id, channel_id, utcnow().isoformat()),
            )
            return cursor.rowcount > 0

    def save(self, channel: NotificationChannelRecord) -> NotificationChannelRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO notification_channels (
                    id, name, channel_type, config_ciphertext, key_version, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel.id,
                    channel.name,
                    channel.channel_type,
                    channel.config_ciphertext,
                    channel.key_version,
                    int(channel.enabled),
                    channel.created_at.isoformat(),
                    channel.updated_at.isoformat(),
                ),
            )
        return channel

    def get(self, channel_id: str) -> Optional[NotificationChannelRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM notification_channels WHERE id = ?", (channel_id,)
            ).fetchone()
        return self._row_to_channel(row) if row else None

    def list(self) -> List[NotificationChannelRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM notification_channels ORDER BY created_at ASC, id ASC"
            ).fetchall()
        return [self._row_to_channel(row) for row in rows]

    def delete(self, channel_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM notification_channels WHERE id = ?", (channel_id,))
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_channel(row: sqlite3.Row) -> NotificationChannelRecord:
        return NotificationChannelRecord(
            id=row["id"],
            name=row["name"],
            channel_type=row["channel_type"],
            config_ciphertext=row["config_ciphertext"],
            key_version=row["key_version"],
            enabled=bool(row["enabled"]),
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )


class SQLiteSnapshotRepository(SQLiteRepositoryBase, SnapshotRepository):
    def replace_for_target(self, target_id: str, snapshots: List[SnapshotRecord]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM snapshots WHERE target_id = ?", (target_id,))
            for snapshot in snapshots:
                connection.execute(
                    """
                    INSERT INTO snapshots (
                        id, target_id, worker_id, snapshot_id, created_at, hostname, paths_json, tags_json, summary_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.id,
                        snapshot.target_id,
                        snapshot.worker_id,
                        snapshot.snapshot_id,
                        snapshot.created_at.isoformat(),
                        snapshot.hostname,
                        json.dumps(snapshot.paths),
                        json.dumps(snapshot.tags),
                        json.dumps(snapshot.summary),
                        snapshot.updated_at.isoformat(),
                    ),
                )

    def list_by_target(self, target_id: str) -> List[SnapshotRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM snapshots WHERE target_id = ? ORDER BY created_at DESC",
                (target_id,),
            ).fetchall()
        return [self._row_to_snapshot(row) for row in rows]

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> SnapshotRecord:
        return SnapshotRecord(
            id=row["id"],
            target_id=row["target_id"],
            worker_id=row["worker_id"],
            snapshot_id=row["snapshot_id"],
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            hostname=row["hostname"],
            paths=_json_load(row["paths_json"], []),
            tags=_json_load(row["tags_json"], []),
            summary=_json_load(row["summary_json"], {}),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )


class SQLiteRetentionPolicyRepository(SQLiteRepositoryBase, RetentionPolicyRepository):
    def save(self, policy: RetentionPolicyRecord) -> RetentionPolicyRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO retention_policies (
                    id, name, keep_last, keep_hourly, keep_daily, keep_weekly, keep_monthly, keep_yearly,
                    prune, labels_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy.id,
                    policy.name,
                    policy.keep_last,
                    policy.keep_hourly,
                    policy.keep_daily,
                    policy.keep_weekly,
                    policy.keep_monthly,
                    policy.keep_yearly,
                    1 if policy.prune else 0,
                    json.dumps(policy.labels),
                    policy.created_at.isoformat(),
                    policy.updated_at.isoformat(),
                ),
            )
        return policy

    def get(self, policy_id: str) -> Optional[RetentionPolicyRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM retention_policies WHERE id = ?", (policy_id,)).fetchone()
        return self._row_to_policy(row) if row else None

    def list(self) -> List[RetentionPolicyRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM retention_policies ORDER BY created_at ASC").fetchall()
        return [self._row_to_policy(row) for row in rows]

    @staticmethod
    def _row_to_policy(row: sqlite3.Row) -> RetentionPolicyRecord:
        return RetentionPolicyRecord(
            id=row["id"],
            name=row["name"],
            keep_last=row["keep_last"],
            keep_hourly=row["keep_hourly"],
            keep_daily=row["keep_daily"],
            keep_weekly=row["keep_weekly"],
            keep_monthly=row["keep_monthly"],
            keep_yearly=row["keep_yearly"],
            prune=bool(row["prune"]),
            labels=_json_load(row["labels_json"], {}),
            created_at=_dt(row["created_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )


class SQLiteTargetStatsRepository(SQLiteRepositoryBase, TargetStatsRepository):
    def save(self, stats_record: TargetStatsRecord) -> TargetStatsRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO target_stats (
                    id, target_id, worker_id, stats_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    stats_record.id,
                    stats_record.target_id,
                    stats_record.worker_id,
                    json.dumps(stats_record.stats),
                    stats_record.updated_at.isoformat(),
                ),
            )
        return stats_record

    def get_by_target(self, target_id: str) -> Optional[TargetStatsRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM target_stats WHERE target_id = ?", (target_id,)).fetchone()
        if not row:
            return None
        return TargetStatsRecord(
            id=row["id"],
            target_id=row["target_id"],
            worker_id=row["worker_id"],
            stats=_json_load(row["stats_json"], {}),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )

    def delete_by_worker(self, worker_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM target_stats WHERE worker_id = ?", (worker_id,))
            return cursor.rowcount > 0


class SQLiteWorkerScheduleClaimRepository(SQLiteRepositoryBase, WorkerScheduleClaimRepository):
    """One durable claim row per worker; refreshed while the worker polls."""

    def save(self, claim: WorkerScheduleClaimRecord) -> WorkerScheduleClaimRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO worker_schedule_claims (
                    worker_id, revision, target_ids_json, errors_json, claimed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    claim.worker_id,
                    claim.revision,
                    json.dumps(claim.target_ids),
                    json.dumps(claim.errors),
                    claim.claimed_at.isoformat(),
                    claim.updated_at.isoformat(),
                ),
            )
        return claim

    def get_by_worker(self, worker_id: str) -> Optional[WorkerScheduleClaimRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worker_schedule_claims WHERE worker_id = ?", (worker_id,)
            ).fetchone()
        if not row:
            return None
        return WorkerScheduleClaimRecord(
            worker_id=row["worker_id"],
            revision=row["revision"],
            target_ids=_json_load(row["target_ids_json"], []),
            errors=_json_load(row["errors_json"], {}),
            claimed_at=_dt(row["claimed_at"]) or datetime.utcnow(),
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )

    def list(self) -> List[WorkerScheduleClaimRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT worker_id FROM worker_schedule_claims ORDER BY claimed_at"
            ).fetchall()
        claims = [self.get_by_worker(row["worker_id"]) for row in rows]
        return [claim for claim in claims if claim is not None]

    def delete_by_worker(self, worker_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM worker_schedule_claims WHERE worker_id = ?", (worker_id,)
            )
            return cursor.rowcount > 0


class SQLiteDashboardReadSnapshotRepository(SQLiteRepositoryBase, DashboardReadSnapshotRepository):
    """Atomic persisted last-good read-model snapshots.

    Only the read-model maintenance lane writes here; a single INSERT OR
    REPLACE per successful refresh keeps each payload whole and readers never
    wait on this store (they serve the in-process RAM copy).
    """

    def save(self, record: DashboardReadSnapshotRecord) -> None:
        with self._lock, self._connect() as connection:
            # One statement per refresh: the persisted payload is atomic.
            connection.execute(
                """
                INSERT OR REPLACE INTO dashboard_read_snapshots (
                    storage_key, kind, tz_offset_minutes, payload_json, generated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.storage_key(),
                    record.kind,
                    record.tz_offset_minutes,
                    # Exact conversion, no default=str: a non-serializable
                    # payload must surface as a refresh failure (last-good kept)
                    # instead of silently stringifying hidden errors.
                    json.dumps(record.payload),
                    record.generated_at.isoformat(),
                ),
            )

    def load_all(self) -> List[DashboardReadSnapshotRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT storage_key, kind, tz_offset_minutes, payload_json, generated_at "
                "FROM dashboard_read_snapshots ORDER BY storage_key"
            ).fetchall()
        records: List[DashboardReadSnapshotRecord] = []
        for row in rows:
            try:
                payload = _json_load(row["payload_json"], None)
                generated_at = _dt(row["generated_at"])
                if not isinstance(payload, dict) or generated_at is None:
                    raise ValueError("snapshot row is incomplete")
            except (TypeError, ValueError, json.JSONDecodeError):
                logger.warning(
                    "Skipping corrupt dashboard read snapshot row for key %s", row["storage_key"]
                )
                continue
            records.append(
                DashboardReadSnapshotRecord(
                    kind=row["kind"],
                    tz_offset_minutes=row["tz_offset_minutes"],
                    payload=payload,
                    generated_at=generated_at,
                )
            )
        return records


class SQLiteSettingsRepository(SQLiteRepositoryBase, SettingsRepository):
    def get(self) -> Optional[SettingsRecord]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM settings WHERE id = 'default'").fetchone()
        if not row:
            return None
        stored_listing_limit = (
            row["snapshot_explorer_listing_max_output_bytes"]
            if "snapshot_explorer_listing_max_output_bytes" in row.keys()
            else None
        )
        if (
            isinstance(stored_listing_limit, bool)
            or not isinstance(stored_listing_limit, int)
            or stored_listing_limit < SettingsRecord.MIN_SNAPSHOT_EXPLORER_LISTING_MAX_OUTPUT_BYTES
            or stored_listing_limit > SettingsRecord.MAX_SNAPSHOT_EXPLORER_LISTING_MAX_OUTPUT_BYTES
        ):
            stored_listing_limit = SettingsRecord.DEFAULT_SNAPSHOT_EXPLORER_LISTING_MAX_OUTPUT_BYTES
        stored_maintenance_days = self._stored_maintenance_days(row)
        stored_quota_gib = row["job_log_quota_gib"] if "job_log_quota_gib" in row.keys() else None
        if (
            isinstance(stored_quota_gib, bool)
            or not isinstance(stored_quota_gib, int)
            or stored_quota_gib < SettingsRecord.MIN_JOB_LOG_QUOTA_GIB
            or stored_quota_gib > SettingsRecord.MAX_JOB_LOG_QUOTA_GIB
        ):
            stored_quota_gib = None
        stored_quota_enabled = row["job_log_quota_enabled"] if "job_log_quota_enabled" in row.keys() else None
        if isinstance(stored_quota_enabled, int):
            stored_quota_enabled = bool(stored_quota_enabled) if stored_quota_enabled in (0, 1) else None
        elif stored_quota_enabled is not None:
            stored_quota_enabled = None
        return SettingsRecord(
            id=row["id"],
            restic_repository_base=row["restic_repository_base"] or "",
            restic_password_secret_id=row["restic_password_secret_id"],
            rclone_conf_secret_id=row["rclone_conf_secret_id"],
            global_cron_expression=row["global_cron_expression"] if "global_cron_expression" in row.keys() else None,
            control_plane_public_url=row["control_plane_public_url"] if "control_plane_public_url" in row.keys() else "",
            snapshot_explorer_listing_max_output_bytes=stored_listing_limit,
            job_log_maintenance_enabled=bool(row["job_log_maintenance_enabled"]) if "job_log_maintenance_enabled" in row.keys() else False,
            job_log_archive_after_days=stored_maintenance_days[0],
            job_log_retention_days=stored_maintenance_days[1],
            job_log_quota_gib=stored_quota_gib,
            job_log_quota_enabled=stored_quota_enabled,
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )

    @staticmethod
    def _stored_maintenance_days(row) -> tuple:
        """Validated (archive_days, retention_days) from the stored row."""
        values = []
        for column, default in (
            ("job_log_archive_after_days", SettingsRecord.DEFAULT_JOB_LOG_ARCHIVE_AFTER_DAYS),
            ("job_log_retention_days", SettingsRecord.DEFAULT_JOB_LOG_RETENTION_DAYS),
        ):
            stored = row[column] if column in row.keys() else None
            if (
                isinstance(stored, bool)
                or not isinstance(stored, int)
                or stored < SettingsRecord.MIN_JOB_LOG_MAINTENANCE_DAYS
                or stored > SettingsRecord.MAX_JOB_LOG_MAINTENANCE_DAYS
            ):
                stored = default
            values.append(stored)
        return tuple(values)

    def save(self, settings: SettingsRecord) -> SettingsRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO settings (
                    id, restic_repository_base, restic_password_secret_id, rclone_conf_secret_id,
                    global_cron_expression, control_plane_public_url,
                    snapshot_explorer_listing_max_output_bytes,
                    job_log_maintenance_enabled, job_log_archive_after_days,
                    job_log_retention_days, job_log_quota_gib, job_log_quota_enabled, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    settings.id,
                    settings.restic_repository_base,
                    settings.restic_password_secret_id,
                    settings.rclone_conf_secret_id,
                    settings.global_cron_expression,
                    settings.control_plane_public_url,
                    settings.snapshot_explorer_listing_max_output_bytes,
                    1 if settings.job_log_maintenance_enabled else 0,
                    settings.job_log_archive_after_days,
                    settings.job_log_retention_days,
                    settings.job_log_quota_gib,
                    None if settings.job_log_quota_enabled is None else (1 if settings.job_log_quota_enabled else 0),
                    settings.updated_at.isoformat(),
                ),
            )
        return settings


class SQLiteCacheRepository(SQLiteRepositoryBase, CacheRepository):
    def get_generation(self, target_id: str, repository_fingerprint: str) -> Optional[CacheGenerationRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM explorer_cache_generations WHERE target_id = ? AND repository_fingerprint = ?",
                (target_id, repository_fingerprint),
            ).fetchone()
        if not row:
            return None
        return CacheGenerationRecord(
            target_id=row["target_id"],
            repository_fingerprint=row["repository_fingerprint"],
            generation=row["generation"] or 0,
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )

    def bump_generation(self, target_id: str, repository_fingerprint: str) -> CacheGenerationRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT generation FROM explorer_cache_generations WHERE target_id = ? AND repository_fingerprint = ?",
                (target_id, repository_fingerprint),
            ).fetchone()
            generation = (row["generation"] if row else 0) + 1
            now = utcnow()
            connection.execute(
                """
                INSERT OR REPLACE INTO explorer_cache_generations (target_id, repository_fingerprint, generation, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (target_id, repository_fingerprint, generation, now.isoformat()),
            )
        return CacheGenerationRecord(
            target_id=target_id,
            repository_fingerprint=repository_fingerprint,
            generation=generation,
            updated_at=now,
        )

    def cleanup_orphaned(self, active_keys: List[Tuple[str, str]]) -> int:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT target_id, repository_fingerprint FROM explorer_cache_generations"
            ).fetchall()
            active = set(active_keys)
            removed = 0
            for row in rows:
                key = (row["target_id"], row["repository_fingerprint"])
                if key not in active:
                    connection.execute(
                        "DELETE FROM explorer_cache_generations WHERE target_id = ? AND repository_fingerprint = ?",
                        key,
                    )
                    removed += 1
        return removed


class SQLiteIndexRepository(SQLiteRepositoryBase, IndexRepository):
    def upsert_status(self, record: IndexStatusRecord) -> IndexStatusRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO explorer_index_status (target_id, snapshot_id, status, entry_count, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    record.target_id,
                    record.snapshot_id,
                    record.status,
                    record.entry_count,
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def get_status(self, target_id: str, snapshot_id: str) -> Optional[IndexStatusRecord]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM explorer_index_status WHERE target_id = ? AND snapshot_id = ?",
                (target_id, snapshot_id),
            ).fetchone()
        if not row:
            return None
        return IndexStatusRecord(
            target_id=row["target_id"],
            snapshot_id=row["snapshot_id"],
            status=row["status"],
            entry_count=row["entry_count"] or 0,
            updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
        )

    def list_by_target(self, target_id: str) -> List[IndexStatusRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM explorer_index_status WHERE target_id = ? ORDER BY snapshot_id ASC",
                (target_id,),
            ).fetchall()
        return [
            IndexStatusRecord(
                target_id=row["target_id"],
                snapshot_id=row["snapshot_id"],
                status=row["status"],
                entry_count=row["entry_count"] or 0,
                updated_at=_dt(row["updated_at"]) or datetime.utcnow(),
            )
            for row in rows
        ]

    def delete_for_target(self, target_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM explorer_index_status WHERE target_id = ?", (target_id,))

    def cleanup_orphaned(self, active_target_ids: List[str]) -> int:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT target_id FROM explorer_index_status").fetchall()
            active = set(active_target_ids)
            removed = 0
            for row in rows:
                if row["target_id"] not in active:
                    connection.execute("DELETE FROM explorer_index_status WHERE target_id = ?", (row["target_id"],))
                    removed += 1
        return removed
