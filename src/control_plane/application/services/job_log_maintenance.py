"""T2 S1-S3 job log lifecycle: stoppable periodic maintenance lane.

Runs only when job-log maintenance is enabled in Global Settings (disabled by
default: the policy deletes retained logs by design). It is deliberately
separate from the scheduler and request lanes:

- one bounded pass per tick: expire-by-retention, then lossless Brotli
  archival at the age cutoff, then — only after a proven quota breach —
  bounded eviction of the oldest finished eligible logs;
- every SQLite write is a bounded batch inside a short transaction, with a
  small cool-down between batches; compression never happens under the
  SQLite write lock (it is delegated to archive_job_log from T1);
- usage is measured by aggregating only the compact per-job log byte sizes,
  never by scanning raw payloads;
- legacy unaccounted sizes are lazily filled by bounded keyset batches while
  the feature is enabled, so the quota total is a hard lower bound (partial /
  eventual enforcement is documented in the Global Settings card and the
  deployment README) and eviction never triggers from incomplete totals
  unless exceedance is proven;
- jobs, summaries, snapshots and backups are never deleted, and
  still-late-write-capable synthetic lease-expiry failures are excluded.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from src.control_plane.domain.models import (
    SettingsRecord,
    effective_job_log_quota_enabled,
    utcnow,
)

logger = logging.getLogger(__name__)

# 1 GiB = 2**30 bytes; patched small in tests to exercise quota eviction.
BYTES_PER_GIB = 1024 ** 3

DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 300
MIN_MAINTENANCE_INTERVAL_SECONDS = 30
MAX_MAINTENANCE_INTERVAL_SECONDS = 86400
DEFAULT_BATCH_SIZE = 25
# Bounded cool-down between batches inside one pass; keeps the SQLite writer
# lane responsive for dispatch/progress traffic.
BATCH_COOLDOWN_SECONDS = 0.25
# Explicit writer-lock limits (B1): every maintenance write transaction opens
# BEGIN IMMEDIATE and touches at most `batch_size` SELECTED rows (one cheap
# per-row recheck plus one atomic predicate write each; batch max 200 rows,
# default 25) under the repository lock and the shared SQLite busy timeout of
# 5 s. Compression is never under the write lock, and selection/or-ordering
# never dereference wide jobs rows. A disable that lands mid-pass therefore
# lets exactly one in-flight bounded batch finish; no pass grows unboundedly.
MAX_BATCH_ROWS = 200
# A candidate whose log could not shrink (compression saved nothing) or whose
# removal predicate failed is retried after this many ticks, not every tick,
# and never starves other candidates. Bounded map; pruned as ticks advance.
CANDIDATE_RETRY_AFTER_TICKS = 6
MAX_CANDIDATE_COOLDOWNS = 512
# Max eviction batches per pass; the next tick keeps evicting if needed.
MAX_QUOTA_EVICT_PASSES = 4
# T3 S4: confirmed prune priority -- bounded prune batches per tick run first
# (and alone) while a prune is pending; the next tick keeps pruning.
MAX_PRUNE_BATCHES_PER_PASS = 4
_REQUIRED_PRUNE_METHODS = (
    "request_job_log_prune",
    "run_job_log_prune_batch",
    "job_log_prune_state",
)
# Recent breach events keep the quota alert visible even when automatic
# eviction drops the measured usage back below the quota.
BREACH_EVENT_VISIBILITY_SECONDS = 7 * 86400
# Matches the bounded storage cap enforced by the SQLite repository.
POLICY_EVENT_LIMIT = 20
MAINTENANCE_INTERVAL_ENV = "CONTROL_PLANE_JOB_LOG_MAINTENANCE_INTERVAL_SECONDS"

_REQUIRED_MAINTENANCE_METHODS = (
    "job_log_usage",
    "backfill_job_log_sizes",
    "expire_job_logs",
    "archive_job_log_candidates",
    "archive_job_log",
    "evict_job_logs_for_quota",
    "job_log_policy_events",
    "record_job_log_policy_events",
)


def maintenance_interval_seconds_from_env() -> int:
    raw = os.environ.get(MAINTENANCE_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_MAINTENANCE_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid %s=%r; using the %ss default",
            MAINTENANCE_INTERVAL_ENV,
            raw,
            DEFAULT_MAINTENANCE_INTERVAL_SECONDS,
        )
        return DEFAULT_MAINTENANCE_INTERVAL_SECONDS
    if value < MIN_MAINTENANCE_INTERVAL_SECONDS:
        logger.warning(
            "Clamping %s=%d to the %ss minimum",
            MAINTENANCE_INTERVAL_ENV,
            value,
            MIN_MAINTENANCE_INTERVAL_SECONDS,
        )
        return MIN_MAINTENANCE_INTERVAL_SECONDS
    if value > MAX_MAINTENANCE_INTERVAL_SECONDS:
        return MAX_MAINTENANCE_INTERVAL_SECONDS
    return value


def _bound_batch_size(value: Optional[int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return DEFAULT_BATCH_SIZE
    return min(value, MAX_BATCH_ROWS)


def _iso_or_none(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


class JobLogMaintenanceService:
    """Stoppable lightweight periodic maintenance thread (separate lane)."""

    def __init__(
        self,
        control_plane_service,
        interval_seconds: Optional[int] = None,
        batch_size: Optional[int] = None,
        now_fn=None,
    ):
        self._service = control_plane_service
        self._interval = maintenance_interval_seconds_from_env() if interval_seconds is None else interval_seconds
        self._batch_size = _bound_batch_size(batch_size)
        self._now_fn = now_fn or utcnow
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # One pass at a time: the periodic thread and an explicit run_once
        # never overlap on the same SQLite writer lane.
        self._tick_lock = threading.Lock()
        self._tick_counter = 0
        self._cooldowns: Dict[str, int] = {}
        self._last_run_at: Optional[datetime] = None
        self._last_result: Optional[Dict[str, Any]] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="cp-job-log-maintenance")
        self._thread.start()
        logger.info(
            "Job log maintenance lane started (interval=%ss; the lane stays idle until enabled in Global Settings)",
            self._interval,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("Job log maintenance lane stopped")

    def _run_loop(self) -> None:
        # T3 S5: the archive is automatic, but it starts after a normal cadence
        # delay on boot -- never inline at startup and never as a blocking scan.
        if self._stop_event.wait(self._interval):
            return
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("Job log maintenance tick failed")
            self._stop_event.wait(self._interval)

    # -- backend detection -------------------------------------------------

    @staticmethod
    def _backend(repo) -> str:
        return "sqlite" if all(hasattr(repo, name) for name in _REQUIRED_MAINTENANCE_METHODS) else "memory"

    def _supported(self) -> bool:
        return self._backend(self._service.job_repository) == "sqlite"

    def _unsupported_message(self) -> str:
        return (
            "Job log maintenance is unsupported for the in-memory job repository: "
            "it is nonpersistent, so there is no archived or retained log data to "
            "maintain. Configuration round-trips are still honored."
        )

    # -- periodic pass -----------------------------------------------------

    def run_once(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        if not self._tick_lock.acquire(timeout=5):
            # Another pass is already running inside this process; report and
            # skip to avoid two concurrent bounded writers on one database.
            return {"supported": self._supported(), "enabled": None, "skipped": True}
        try:
            result = self._execute(now if now is not None else self._now_fn())
            self._last_run_at = self._now_fn()
            self._last_result = result
            return result
        finally:
            self._tick_lock.release()

    def _execute(self, now: datetime) -> Dict[str, Any]:
        repo = self._service.job_repository
        if not self._supported():
            return {"supported": False, "enabled": None, "message": self._unsupported_message()}
        settings = self._service.get_settings()
        # A fresh database without a stored settings row uses the defaults, so
        # the automatic archive also applies there (S5); nothing else may act.
        config = settings if settings is not None else SettingsRecord()
        maintenance_enabled = bool(config.job_log_maintenance_enabled)

        self._tick_counter += 1
        self._prune_cooldowns()

        result: Dict[str, Any] = {
            "supported": True,
            "enabled": maintenance_enabled,
            "backfill": None,
            "usage": None,
            "quota_breached": False,
            "actions": {},
            "message": None,
        }

        usage = repo.job_log_usage()
        if not usage["complete"]:
            # Bounded lazy accounting of legacy rows; one batch per tick. It is
            # no longer gated by the retention opt-in (S5): the prune, the
            # archive and the quota all need complete compact coverage first.
            result["backfill"] = repo.backfill_job_log_sizes(batch_size=self._batch_size)
            usage = repo.job_log_usage()
        result["usage"] = usage

        actions: Dict[str, Any] = result["actions"]

        # T3 S4 priority: a confirmed cutoff prune runs first and alone, across
        # subsequent batches and restarts, until its persisted state completes.
        prune = self._run_prune_batches(repo, now)
        if prune is not None:
            actions["pruned"] = len(prune["removed"])
            actions["pruned_bytes_freed"] = prune["bytes_freed"]
            result["prune"] = prune
            if prune["state"].get("status") == "pending":
                # Defer the scheduled retention/quota/archive phases until the
                # confirmed prune finishes; the next tick resumes with priority.
                return result

        # Phase 1: expire eligible storage first (S2), now ONLY behind the
        # retention opt-in: finished jobs older than the retention age lose
        # their stored log payload entirely. Per-row recheck skips (e.g.
        # late-report-capable synthetic failures the compact selection cannot
        # filter without a summary scan) are cooled down so they never consume
        # batch slots every tick.
        retention_days = getattr(config, "job_log_retention_days", None)
        if maintenance_enabled:
            archive_days = getattr(config, "job_log_archive_after_days", None)
            if (
                isinstance(retention_days, bool)
                or not isinstance(retention_days, int)
                or retention_days < 1
                or isinstance(archive_days, bool)
                or not isinstance(archive_days, int)
                or archive_days < 1
                or archive_days >= retention_days
            ):
                logger.error("Job log retention configuration is invalid; the retention pass did nothing")
                result["message"] = "invalid_config"
            else:
                expired = repo.expire_job_logs(
                    now - timedelta(days=retention_days),
                    self._batch_size,
                    exclude_ids=self._cooled_job_ids(),
                )
                actions["expired"] = expired["expired"]
                actions["expired_bytes_freed"] = expired["bytes_freed"]
                for job_id in expired.get("skipped", []):
                    self._cooldown(job_id)
                if expired["expired"]:
                    repo.record_job_log_policy_events([{
                        "at": now.isoformat(),
                        "kind": "logs_expired",
                        "job_count": expired["expired"],
                        "bytes_freed": expired["bytes_freed"],
                        "job_ids": expired["jobs"][:5],
                    }])
                    time.sleep(BATCH_COOLDOWN_SECONDS)

        # Phase 2: lossless archive at the age cutoff (S1), now AUTOMATIC (S5):
        # it runs regardless of the retention opt-in and the quota, at the
        # stored archive age (30 by default). It only compresses; it never
        # deletes and never implicitly authorizes deletion.
        archive_days = getattr(config, "job_log_archive_after_days", None)
        if isinstance(archive_days, bool) or not isinstance(archive_days, int) or archive_days < 1:
            logger.error("Job log archive age configuration is invalid; the archive pass did nothing")
            result["message"] = result["message"] or "invalid_config"
        else:
            archived_count = 0
            archived_saved = 0
            archive_skipped = 0
            candidates = repo.archive_job_log_candidates(
                now - timedelta(days=archive_days),
                self._batch_size,
                exclude_ids=self._cooled_job_ids(),
            )
            for job_id in candidates[: self._batch_size]:
                saved = repo.archive_job_log(job_id, now - timedelta(days=archive_days))
                if saved and saved > 0:
                    archived_count += 1
                    archived_saved += saved
                else:
                    # It did not shrink (or lost the race): cool it down rather
                    # than retrying it every tick or starving other candidates.
                    archive_skipped += 1
                    self._cooldown(job_id)
                time.sleep(BATCH_COOLDOWN_SECONDS)
            actions["archived"] = archived_count
            actions["archived_bytes_saved"] = archived_saved
            actions["archive_skipped"] = archive_skipped
            if archived_count:
                repo.record_job_log_policy_events([{
                    "at": now.isoformat(),
                    "kind": "logs_archived",
                    "job_count": archived_count,
                    "bytes_freed": archived_saved,
                }])

        # Phase 3: quota (S3) -- active through its own explicit flag, or the
        # legacy effective default (`maintenance_enabled AND configured`).
        # Only after a proven breach of the measured compact total, which is a
        # lower bound while the backfill is incomplete; exceedance is thus
        # proven, never estimated.
        quota_breached = False
        if effective_job_log_quota_enabled(config):
            quota_gib = getattr(config, "job_log_quota_gib", None)
            if isinstance(quota_gib, int) and not isinstance(quota_gib, bool) and quota_gib > 0:
                quota_bytes = quota_gib * BYTES_PER_GIB
                usage = repo.job_log_usage()
                # Strict breach (B2): a total exactly AT the quota never deletes.
                if usage["measured_bytes"] > quota_bytes:
                    quota_breached = True
                    actions["quota_breached"] = True
                    logger.warning(
                        "Job log quota exceeded: measured job log payload usage %d bytes "
                        "> quota %d bytes; the maintenance lane is evicting the oldest "
                        "finished eligible job logs (jobs, summaries and backups are kept)",
                        usage["measured_bytes"],
                        quota_bytes,
                    )
                    repo.record_job_log_policy_events([{
                        "at": now.isoformat(),
                        "kind": "quota_breach",
                        "measured_bytes": usage["measured_bytes"],
                        "quota_gib": quota_gib,
                    }])
                    evicted: list = []
                    evicted_bytes = 0
                    for _ in range(MAX_QUOTA_EVICT_PASSES):
                        evicted_batch = repo.evict_job_logs_for_quota(
                            self._batch_size,
                            exclude_ids=self._cooled_job_ids(),
                            # B2: the byte budget is required; the repository
                            # re-measures inside its transaction and stops the
                            # moment the total is <= budget (per deletion).
                            free_to_bytes=quota_bytes,
                        )
                        for job_id in evicted_batch.get("skipped", []):
                            self._cooldown(job_id)
                        if not evicted_batch["jobs"]:
                            break
                        evicted.extend(evicted_batch["jobs"])
                        evicted_bytes += evicted_batch["bytes_freed"]
                        usage = repo.job_log_usage()
                        if usage["measured_bytes"] <= quota_bytes:
                            break
                        time.sleep(BATCH_COOLDOWN_SECONDS)
                    actions["evicted"] = len(evicted)
                    actions["evicted_bytes_freed"] = evicted_bytes
                    if evicted:
                        repo.record_job_log_policy_events([{
                            "at": now.isoformat(),
                            "kind": "logs_evicted",
                            "job_count": len(evicted),
                            "bytes_freed": evicted_bytes,
                            "job_ids": evicted[:5],
                        }])

        result["quota_breached"] = quota_breached
        return result

    # -- confirmed prune priority (T3 S4) ----------------------------------

    def _run_prune_batches(self, repo, now: datetime) -> Optional[Dict[str, Any]]:
        """Advance a pending confirmed prune by bounded batches, or None.

        Returns None when there is no pending prune state: the scheduled
        phases then run. While a prune is pending, at most MAX_PRUNE_BATCHES_
        PER_PASS bounded batches run per tick and the caller defers the other
        phases; the persisted state makes this resumable after a restart.
        """
        if not all(hasattr(repo, name) for name in _REQUIRED_PRUNE_METHODS):
            return None
        state = repo.job_log_prune_state()
        if not state or state.get("status") != "pending":
            return None
        aggregate: Dict[str, Any] = {
            "state": state,
            "removed": [],
            "bytes_freed": 0,
            "batches": 0,
        }
        for _ in range(MAX_PRUNE_BATCHES_PER_PASS):
            if self._stop_event.is_set():
                break
            batch = repo.run_job_log_prune_batch(self._batch_size, now=now)
            aggregate["batches"] += 1
            aggregate["removed"].extend(batch["removed"])
            aggregate["bytes_freed"] += batch["bytes_freed"]
            aggregate["state"] = batch["state"]
            if not batch["active"] or batch["complete"]:
                break
            time.sleep(BATCH_COOLDOWN_SECONDS)
        return aggregate

    # -- candidate cooldown -------------------------------------------------

    def _cooldown(self, job_id: str) -> None:
        self._cooldowns[job_id] = self._tick_counter + CANDIDATE_RETRY_AFTER_TICKS
        if len(self._cooldowns) > MAX_CANDIDATE_COOLDOWNS:
            for cooled_id, retry_at in sorted(self._cooldowns.items(), key=lambda item: item[1]):
                del self._cooldowns[cooled_id]
                if len(self._cooldowns) <= MAX_CANDIDATE_COOLDOWNS:
                    break

    def _cooled_job_ids(self) -> set:
        return set(self._cooldowns)

    def _prune_cooldowns(self) -> None:
        current = self._tick_counter
        self._cooldowns = {
            job_id: retry_at
            for job_id, retry_at in self._cooldowns.items()
            if retry_at > current
        }

    # -- observability -------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Measured usage, recent breach/eviction events and lane metrics."""
        service = self._service
        repo = service.job_repository
        settings = service.get_settings()
        supported = self._supported()
        payload: Dict[str, Any] = {
            "supported": supported,
            "interval_seconds": self._interval,
            "batch_size": self._batch_size,
            "enabled": bool(settings.job_log_maintenance_enabled) if settings is not None else False,
            "archive_after_days": getattr(settings, "job_log_archive_after_days", None) if settings is not None else None,
            "retention_days": getattr(settings, "job_log_retention_days", None) if settings is not None else None,
            "quota_gib": getattr(settings, "job_log_quota_gib", None) if settings is not None else None,
            "last_run_at": _iso_or_none(self._last_run_at),
            "last_result": self._last_result,
            "batch_rows_max": MAX_BATCH_ROWS,
        }
        if not supported:
            payload.update({
                "message": self._unsupported_message(),
                "usage": None,
                "quota_breached": False,
                "events": [],
            })
            return payload
        usage = repo.job_log_usage()
        events = repo.job_log_policy_events()
        quota_gib = getattr(settings, "job_log_quota_gib", None) if settings is not None else None
        quota_breached = False
        if quota_gib is not None:
            # Strict breach (B2): exactly-at-quota is not an excess.
            if usage["measured_bytes"] > quota_gib * BYTES_PER_GIB:
                quota_breached = True
            else:
                # Keep the trigger visible after automatic eviction dropped
                # the measured usage back below the quota.
                now = self._now_fn()
                now_naive = now.replace(tzinfo=None) if now.tzinfo else now
                for event in events:
                    if event.get("kind") != "quota_breach":
                        continue
                    try:
                        at = datetime.fromisoformat(event.get("at"))
                        if at.tzinfo:
                            at = at.astimezone(now.tzinfo or None)
                    except (TypeError, ValueError):
                        continue
                    if (now_naive - (at.replace(tzinfo=None) if at.tzinfo else at)) <= timedelta(
                        seconds=BREACH_EVENT_VISIBILITY_SECONDS
                    ):
                        quota_breached = True
                        break
        payload.update({
            "usage": usage,
            "quota_breached": quota_breached,
            "events": events[-POLICY_EVENT_LIMIT:],
            "message": None,
        })
        return payload
