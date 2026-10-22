"""Coalesced dashboard read models (dashboard-read-models T2, spec S1-S4).

Read requests serve a last-good snapshot straight from process RAM: they never
build, never query the authoritative jobs/targets/inventory/settings stores,
never reconcile leases and never block on a refresh or storage lock while a
build or database write is running. A single maintenance lane (one bounded,
coalesced queue with one pending/in-flight refresh per key) refreshes only the
active/loaded keys.

Successful snapshots are persisted atomically and published under a short
lock; a failed rebuild (builder or storage error) concludes the pending
refresh, keeps the last-good copy and retries on a bounded cadence instead of
on every failed read. A key with no snapshot at all reports explicit warm-up
instead of faked zero/empty data. On restart the last-good copies are hydrated
from SQLite so cold-start warm-up converges without rebuilding or a browser
reload.
"""

import copy
import logging
import math
import threading
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.control_plane.domain.models import DashboardReadSnapshotRecord, utcnow

logger = logging.getLogger(__name__)

DASHBOARD_STATS_KIND = "stats"
DASHBOARD_TARGETS_KIND = "targets"
READ_MODEL_WARMUP_CODE = "read_model_warming_up"
DEFAULT_REFRESH_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_TZ_OFFSET_KEYS = 32
# Same bound as the HTTP tz_offset contract; the service re-validates as a backstop.
JOB_STATS_MIN_TZ_OFFSET_MINUTES = -14 * 60
JOB_STATS_MAX_TZ_OFFSET_MINUTES = 14 * 60


def _validated_refresh_interval(value) -> float:
    try:
        interval = float(value)
    except (TypeError, ValueError):
        raise ValueError("refresh_interval_seconds must be a finite positive number") from None
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("refresh_interval_seconds must be a finite positive number")
    return interval


@dataclass
class DashboardReadResult:
    """Read outcome for one dashboard read-model key."""

    kind: str
    key: str
    warming_up: bool
    snapshot: Optional[DashboardReadSnapshotRecord]
    retry_after_seconds: int
    pending_refresh_event: Optional[threading.Event]


@dataclass
class _KeyState:
    """Per-key refresh bookkeeping; the service lock is never held while building.

    State objects carry identity: an evicted key that is re-added gets a fresh
    state object, so an older in-flight build can never republish into the new
    state. Refreshes conclude (set) their event even when the key was evicted
    mid-build, so waiters never hang.
    """

    snapshot: Optional[DashboardReadSnapshotRecord] = None
    queued: bool = False
    refresh_event: Optional[threading.Event] = None
    last_error: Optional[str] = None
    retry_after: Optional[datetime] = None  # bounded retry cadence after failures


class DashboardReadService:
    """Serves dashboard reads from last-good snapshots refreshed by one maintenance lane."""

    def __init__(
        self,
        snapshot_repository,
        stats_builder: Optional[Callable[[int], Dict[str, Any]]] = None,
        targets_builder: Optional[Callable[[], Dict[str, Any]]] = None,
        refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
        max_tz_offset_keys: int = DEFAULT_MAX_TZ_OFFSET_KEYS,
        now_fn: Optional[Callable[[], datetime]] = None,
    ):
        self._repository = snapshot_repository
        self._stats_builder = stats_builder
        self._targets_builder = targets_builder
        self._interval = _validated_refresh_interval(refresh_interval_seconds)
        self._max_tz_offset_keys = max(1, int(max_tz_offset_keys))
        self._now_fn = now_fn or utcnow
        self._cond = threading.Condition()
        self._states: Dict[Tuple[str, Optional[int]], _KeyState] = {}
        self._queue: deque = deque()  # entries of (key, state); identity-checked on drain
        self._offset_order: Dict[int, None] = {}  # LRU of touched tz offsets
        self._drain_budget = 4 * (self._max_tz_offset_keys + 1)
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hydrate()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self.is_running():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="cp-dashboard-read")
        self._thread.start()
        logger.info("Dashboard read-model lane started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        thread = self._thread
        if thread:
            thread.join(timeout=10)
        if thread and thread.is_alive():
            logger.warning("Dashboard read-model lane is still finishing its current refresh")
        else:
            self._thread = None
            logger.info("Dashboard read-model lane stopped")

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def prime(self, default_tz_offset_minutes: int = 0) -> None:
        """Queue an asynchronous first refresh for the targets key and default tz offset.

        The maintenance lane builds in the background; startup never blocks on
        a build, and no other timezone keys are rebuilt arbitrarily.
        """
        if self._targets_builder is not None:
            self.read_targets()
        if self._stats_builder is not None:
            self.read_stats(default_tz_offset_minutes)

    # -- public read API ---------------------------------------------------

    def read_stats(self, tz_offset_minutes: int) -> DashboardReadResult:
        """Serve the stats read model for an exact minute tz offset.

        Never builds and never blocks on the refresh lane; re-validates the
        `-840..840` HTTP contract as a backstop.
        """
        try:
            offset = int(tz_offset_minutes)
        except (TypeError, ValueError):
            raise ValueError("invalid tz_offset: must be an integer") from None
        if not JOB_STATS_MIN_TZ_OFFSET_MINUTES <= offset <= JOB_STATS_MAX_TZ_OFFSET_MINUTES:
            raise ValueError(
                f"invalid tz_offset: must be between "
                f"{JOB_STATS_MIN_TZ_OFFSET_MINUTES} and {JOB_STATS_MAX_TZ_OFFSET_MINUTES}"
            )
        key = self._key(DASHBOARD_STATS_KIND, offset)
        return self._read(key, f"stats:{offset}", touch_offset=offset)

    def read_targets(self) -> DashboardReadResult:
        """Serve the enriched target list from the last-good snapshot."""
        if self._targets_builder is None:
            raise ValueError("dashboard read model has no targets builder")
        key = self._key(DASHBOARD_TARGETS_KIND, None)
        return self._read(key, DASHBOARD_TARGETS_KIND, touch_offset=None)

    def current_snapshot(self, kind: str, tz_offset_minutes: Optional[int] = None) -> Optional[DashboardReadSnapshotRecord]:
        """Return a copy of the in-memory last-good snapshot for a key (introspection/tests)."""
        with self._cond:
            state = self._states.get(self._key(kind, tz_offset_minutes))
            record = state.snapshot if state else None
        return self._copied_snapshot(record)

    def age_seconds(self, snapshot: DashboardReadSnapshotRecord) -> int:
        """Freshness age in whole seconds for the freshness headers (naive UTC ``generated_at``)."""
        return max(0, int((self._now_fn() - snapshot.generated_at).total_seconds()))

    # -- internals ---------------------------------------------------------

    def _key(self, kind: str, tz_offset_minutes: Optional[int]) -> Tuple[str, Optional[int]]:
        return (kind, tz_offset_minutes if kind == DASHBOARD_STATS_KIND else None)

    def _copied_snapshot(self, record) -> Optional[DashboardReadSnapshotRecord]:
        """Copy the payload outside the short lock so callers cannot mutate state."""
        if record is None:
            return None
        return replace(record, payload=copy.deepcopy(record.payload))

    def _read(self, key, record_key, touch_offset) -> DashboardReadResult:
        enqueue_requested = False
        retry_after = max(1, int(math.ceil(self._interval)))
        with self._cond:
            state = self._touch_locked(touch_offset)
            if state is None:
                state = self._states.setdefault(key, _KeyState())
            snapshot_ref = state.snapshot
            stale = snapshot_ref is None or self._age_seconds_locked(snapshot_ref) >= self._interval
            cadence_blocked = self._retry_cadence_active_locked(state)
            if stale and not state.queued and not cadence_blocked:
                self._enqueue_locked(key, state)
                enqueue_requested = True
            pending_event = state.refresh_event if state.queued else None
        if enqueue_requested:
            self._wakeup.set()  # wake the worker without holding the lock
        # Payload copied outside the short lock; the stored record is never mutated.
        snapshot = self._copied_snapshot(snapshot_ref)
        if snapshot is None:
            return DashboardReadResult(
                kind=key[0],
                key=record_key,
                warming_up=True,
                snapshot=None,
                retry_after_seconds=retry_after,
                pending_refresh_event=pending_event,
            )
        return DashboardReadResult(
            kind=key[0],
            key=record_key,
            warming_up=False,
            snapshot=snapshot,
            retry_after_seconds=retry_after,
            pending_refresh_event=pending_event,
        )

    def _touch_locked(self, touch_offset: Optional[int]) -> Optional[_KeyState]:
        """Mark a stats offset active and apply the bounded LRU (targets never evict)."""
        if touch_offset is None:
            return self._states.get((DASHBOARD_TARGETS_KIND, None))
        self._offset_order.pop(touch_offset, None)
        self._offset_order[touch_offset] = None
        while len(self._offset_order) > self._max_tz_offset_keys:
            oldest, _ = next(iter(self._offset_order.items()))
            del self._offset_order[oldest]
            evicted = self._states.pop((DASHBOARD_STATS_KIND, oldest), None)
            if evicted is not None:
                queued_entries = [entry for entry in self._queue if entry[1] is evicted]
                self._queue = deque(entry for entry in self._queue if entry[1] is not evicted)
                if queued_entries and evicted.refresh_event is not None:
                    evicted.queued = False
                    evicted.refresh_event.set()
        return self._states.get((DASHBOARD_STATS_KIND, touch_offset))

    def _enqueue_locked(self, key, state: _KeyState) -> None:
        if state.queued:
            return  # coalesced: at most one pending/in-flight refresh per key
        state.queued = True
        state.refresh_event = threading.Event()
        self._queue.append((key, state))

    def _retry_cadence_active_locked(self, state: _KeyState) -> bool:
        return state.retry_after is not None and self._now_fn() < state.retry_after

    def _age_seconds_locked(self, snapshot) -> float:
        return max(0.0, (self._now_fn() - snapshot.generated_at).total_seconds())

    def _is_due_locked(self, key, state: _KeyState) -> bool:
        if state.queued or self._retry_cadence_active_locked(state):
            return False
        return state.snapshot is None or self._age_seconds_locked(state.snapshot) >= self._interval

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            self._wakeup.wait(self._interval)
            self._wakeup.clear()
            if self._stop.is_set():
                return
            try:
                self._refresh_tick()
            except Exception:
                logger.exception("Dashboard read-model refresh tick failed")

    def _refresh_tick(self) -> None:
        """Queue the due loaded keys, then drain the queue with a bounded budget.

        Invalid entries (evicted states / already-concluded keys) are skipped,
        never aborting the drain, so pending work is not lost.
        """
        with self._cond:
            due = [key for key, state in self._states.items() if self._is_due_locked(key, state)]
            for key in due:
                self._enqueue_locked(key, self._states[key])
        drained = 0
        while drained < self._drain_budget and not self._stop.is_set():
            with self._cond:
                entry = self._queue.popleft() if self._queue else None
                valid = entry is not None and self._states.get(entry[0]) is entry[1] and entry[1].queued
            if entry is None:
                return
            drained += 1
            key, state = entry
            if not valid:
                continue  # evicted/stale entry: prune it and keep draining
            self._run_refresh(key, state)

    def _run_refresh(self, key, state: _KeyState) -> None:
        """Build, persist and publish one refresh outside the service lock.

        Builder and storage errors are handled together: the refresh concludes
        (event always set), the queue slot is released, the prior last-good
        snapshot is preserved and the next retry is bounded by the cadence.
        """
        kind, tz_offset_minutes = key
        try:
            if kind == DASHBOARD_STATS_KIND:
                if self._stats_builder is None:
                    raise RuntimeError("dashboard read model has no stats builder")
                payload = dict(self._stats_builder(tz_offset_minutes))
            else:
                payload = dict(self._targets_builder())
            record = DashboardReadSnapshotRecord(
                kind=kind,
                tz_offset_minutes=tz_offset_minutes if kind == DASHBOARD_STATS_KIND else None,
                payload=payload,
                generated_at=self._now_fn(),
            )
            # Persisted outside the service lock: readers never wait on storage.
            self._repository.save(record)
        except Exception as exc:
            with self._cond:
                state.queued = False
                state.last_error = str(exc)
                # Bounded retry cadence: a failing key is only re-enqueued on a
                # later tick/read after the interval, not on every failed read.
                state.retry_after = self._now_fn() + timedelta(seconds=self._interval)
                event, state.refresh_event = state.refresh_event, None
            # Conclusion first: waiters of the pending refresh must never hang.
            if event is not None:
                event.set()
            logger.warning("Dashboard read-model refresh failed for key %s.%s: %s", kind, tz_offset_minutes, exc)
            return
        with self._cond:
            current = self._states.get(key)
            publishable = current is state  # never republish an evicted state
            if publishable:
                current.queued = False
                current.snapshot = record
                current.last_error = None
                current.retry_after = None
                event = current.refresh_event
                current.refresh_event = None
            else:
                event = state.refresh_event
        # The refresh always concludes so no reader ever waits forever, whether
        # the result was published or the key was evicted mid-build.
        if event is not None:
            event.set()
        if not publishable:
            logger.info("Dropped dashboard read-model result for %s.%s: key was evicted mid-build", kind, tz_offset_minutes)

    def _hydrate(self) -> None:
        """Load persisted last-good snapshots into RAM before serving reads.

        Bounded by the same key cap as the live LRU (the newest rows win, and
        hydrated offsets seed the LRU so they count toward the bound).
        """
        try:
            records = self._repository.load_all()
        except Exception:
            logger.exception("Unable to hydrate dashboard read snapshots")
            return
        with self._cond:
            stats_rows = sorted(
                [record for record in self._valid_hydratable_rows(records) if record.kind == DASHBOARD_STATS_KIND],
                key=lambda record: record.generated_at,
                reverse=True,
            )[: self._max_tz_offset_keys]
            targets_rows = [
                record for record in self._valid_hydratable_rows(records) if record.kind == DASHBOARD_TARGETS_KIND
            ]
            for record in list(reversed(stats_rows)) + targets_rows:
                key = self._key(record.kind, record.tz_offset_minutes)
                state = self._states.setdefault(key, _KeyState())
                if state.snapshot is None or record.generated_at > state.snapshot.generated_at:
                    state.snapshot = record
                if record.kind == DASHBOARD_STATS_KIND:
                    # Hydrated offsets are loaded keys: count them on the LRU so
                    # the bound survives restarts.
                    self._offset_order[record.tz_offset_minutes] = None
                    while len(self._offset_order) > self._max_tz_offset_keys:
                        oldest, _ = next(iter(self._offset_order.items()))
                        del self._offset_order[oldest]
                        self._states.pop((DASHBOARD_STATS_KIND, oldest), None)
        if records:
            logger.info("Dashboard read model hydrated %s snapshot(s)", len(records))

    def _valid_hydratable_rows(self, records: List[DashboardReadSnapshotRecord]) -> List[DashboardReadSnapshotRecord]:
        valid = []
        for record in records:
            if record.kind not in (DASHBOARD_STATS_KIND, DASHBOARD_TARGETS_KIND):
                logger.warning("Skipping persisted snapshot with unknown kind %r", record.kind)
                continue
            if not isinstance(record.payload, dict) or not isinstance(record.generated_at, datetime) or record.generated_at.tzinfo is not None:
                logger.warning("Skipping incomplete persisted snapshot for kind %s", record.kind)
                continue
            if record.kind == DASHBOARD_STATS_KIND:
                offset = record.tz_offset_minutes
                if not isinstance(offset, int) or not JOB_STATS_MIN_TZ_OFFSET_MINUTES <= offset <= JOB_STATS_MAX_TZ_OFFSET_MINUTES:
                    logger.warning("Skipping persisted snapshot with invalid tz offset %r", offset)
                    continue
            valid.append(record)
        return valid