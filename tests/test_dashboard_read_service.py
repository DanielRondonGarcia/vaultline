"""Dashboard read-model tests (dashboard-read-models T2, spec S1-S4).

The dashboard reads were served live from the authoritative stores: every
``GET /api/v1/jobs/stats`` and ``GET /api/v1/targets`` re-executed the full
aggregation/enrichment pipeline. This module pins the persisted read-model
layer:

- reads serve a last-good RAM snapshot hydrated from SQLite on restart,
- reads never build, query authoritative jobs/targets/inventory/settings,
  reconcile leases or block on a refresh/DB lock while a build runs,
- with no snapshot at all the response is an explicit warm-up outcome, never
  faked zero/empty data and never a synchronous expensive fallback,
- successful snapshots are persisted atomically and published under a short
  lock; builder or storage failures conclude the refresh and keep last-good,
  retrying on a bounded cadence instead of on every failed read,
- one coalesced (pending or in-flight) refresh per bounded key queue, with a
  periodic lane that only refreshes active/loaded keys; LRU eviction prunes
  pending work, drops evicted in-flight results and never loses the rest,
- the legacy response bodies (``{total, by_status, by_day}`` and ``{items}``),
  the exact minute-based timezone day cut (including +330 / -300) and the
  ``-840..840`` validation stay intact,
- the main lifecycle starts and stops the maintenance lane without blocking
  startup (priming is asynchronous).

Warm-up, eventual refresh, coalescing, eviction and restart convergence are
asserted through events, immediate reads and finite HTTP request loops, not
sleeps. HTTP tests drive background builds deterministically: they either
pre-persist snapshot rows (hydration contract) or use the service's own
refresh events captured from the original read result, and they release
blocked builder gates in ``finally`` before stopping the lane.
"""

import json
import math
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.application.services.dashboard_read_service import (
    DashboardReadService,
    READ_MODEL_WARMUP_CODE,
)
from src.control_plane.domain.models import (
    DashboardReadSnapshotRecord,
    JobRecord,
    WorkerRecord,
    WorkerStatus,
    utcnow,
)
from src.control_plane.infrastructure.repositories.in_memory import (
    InMemoryDashboardReadSnapshotRepository,
    InMemoryInventoryRepository,
    InMemoryJobRepository,
    InMemoryNotificationChannelRepository,
    InMemoryRetentionPolicyRepository,
    InMemorySecretRepository,
    InMemorySettingsRepository,
    InMemorySnapshotRepository,
    InMemoryStorageProfileRepository,
    InMemoryTargetRepository,
    InMemoryTargetStatsRepository,
    InMemoryWorkerRepository,
)
from src.control_plane.infrastructure.repositories.sqlite import (
    SQLiteDashboardReadSnapshotRepository,
    SQLiteJobRepository,
)
from src.control_plane.application.services.scheduler_service import SchedulerService
from src.control_plane.infrastructure.security.worker_auth import WorkerAuthState
from src.control_plane.main import (
    ControlPlaneApplication,
    ControlPlaneHTTPServer,
    ControlPlaneRequestHandler,
    build_target_view,
    run_control_plane,
)


class _TestClock:
    """Deterministic single-source now() for the service under test."""

    def __init__(self, start=None):
        self.lock = threading.Lock()
        self.value = start or datetime(2026, 1, 15, 12, 0, 0)

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value = self.value + timedelta(seconds=seconds)


class _ControlledBuilder:
    """Counts builds, optionally blocks on a gate and optionally fails late."""

    def __init__(self, payload_factory=None):
        self.lock = threading.Lock()
        self.calls = []
        self.gate = None
        self.entered = threading.Event()  # set when any build begins
        self.fail_after = None  # calls allowed before raising; None never fails
        self.payload_factory = payload_factory or (lambda call_number: {"call": call_number})

    def __call__(self, *builder_args):
        with self.lock:
            self.calls.append(builder_args)
            index = len(self.calls)
            fail = self.fail_after is not None and index > self.fail_after
            gate = self.gate
        self.entered.set()
        if gate is not None:
            gate.wait(15)
        if fail:
            raise RuntimeError("simulated read-model build failure")
        return self.payload_factory(index)

    def count(self):
        with self.lock:
            return len(self.calls)


class _FailingSaveRepository:
    """Snapshot repository whose save can fail; loads pass through."""

    def __init__(self, inner):
        self._inner = inner
        self.fail_saves = False
        self.attempts = 0

    def save(self, record):
        self.attempts += 1
        if self.fail_saves:
            raise RuntimeError("simulated storage write failure")
        return self._inner.save(record)

    def load_all(self):
        return self._inner.load_all()


def _stats_payload_factory(call_number):
    return {"total": call_number, "by_status": {}, "by_day": []}


def _targets_payload_factory(_call_number):
    return {"items": [{"name": f"target-{_call_number}"}]}


def _release_gate(builder):
    """Release a blocked builder gate so the lane can conclude (bounded waits)."""
    gate = builder.gate
    if gate is not None:
        gate.set()


class StatsFixtureBuilder:
    """Stats builder backed by the authoritative repository/service, event-free."""

    def __init__(self, service, fixed_now=None):
        self.service = service
        self.fixed_now = fixed_now
        self.lock = threading.Lock()
        self.calls = []

    def __call__(self, tz_offset_minutes):
        with self.lock:
            self.calls.append(tz_offset_minutes)
        if self.fixed_now is not None:
            # Same compact aggregation the authoritative service serves, with
            # an injected deterministic now (the service itself uses utcnow()).
            return self.service.job_repository.job_stats(
                tz_offset_minutes=tz_offset_minutes, now=self.fixed_now()
            )
        return self.service.job_stats(tz_offset_minutes=tz_offset_minutes)


# Backwards-compatible alias used across the module.
_StatsFixtureBuilder = StatsFixtureBuilder


def _make_control_plane_service(job_repository=None):
    workers = InMemoryWorkerRepository()
    workers.save(
        WorkerRecord(
            name="worker-a",
            host_name="host-a",
            id="worker-a",
            status=WorkerStatus.ONLINE,
            last_seen_at=utcnow(),
        )
    )
    service = ControlPlaneService(
        worker_repository=workers,
        inventory_repository=InMemoryInventoryRepository(),
        target_repository=InMemoryTargetRepository(),
        job_repository=job_repository or InMemoryJobRepository(),
        storage_profile_repository=InMemoryStorageProfileRepository(),
        secret_repository=InMemorySecretRepository(),
        snapshot_repository=InMemorySnapshotRepository(),
        retention_policy_repository=InMemoryRetentionPolicyRepository(),
        target_stats_repository=InMemoryTargetStatsRepository(),
        secret_codec=object(),
        settings_repository=InMemorySettingsRepository(),
        notification_channel_repository=InMemoryNotificationChannelRepository(),
    )
    service.worker_auth = WorkerAuthState()
    return service


def _make_read_service(
    *,
    snapshot_repository=None,
    stats_builder=None,
    targets_builder=None,
    interval=60.0,
    max_tz_offset_keys=32,
    clock="test",
):
    if clock == "test":
        clock = _TestClock()
    service = DashboardReadService(
        snapshot_repository=snapshot_repository or InMemoryDashboardReadSnapshotRepository(),
        stats_builder=stats_builder,
        targets_builder=targets_builder,
        refresh_interval_seconds=interval,
        max_tz_offset_keys=max_tz_offset_keys,
        now_fn=(lambda: clock()) if clock is not None else None,
    )
    return service, clock


class DashboardReadServiceResultTests(unittest.TestCase):
    def test_missing_snapshot_reports_warming_and_eventual_refresh(self):
        stats_builder = _ControlledBuilder(_stats_payload_factory)
        service, _clock = _make_read_service(stats_builder=stats_builder)
        service.start()
        try:
            first = service.read_stats(0)
            self.assertTrue(first.warming_up)
            self.assertIsNone(first.snapshot)
            self.assertEqual(first.retry_after_seconds, math.ceil(60))
            self.assertIsNotNone(first.pending_refresh_event)
            self.assertTrue(first.pending_refresh_event.wait(10))
            second = service.read_stats(0)
            self.assertFalse(second.warming_up)
            self.assertEqual(second.snapshot.payload, {"total": 1, "by_status": {}, "by_day": []})
        finally:
            _release_gate(stats_builder)
            service.stop()

    def test_empty_authoritative_database_is_a_zero_snapshot_not_warming(self):
        with tempfile.TemporaryDirectory() as directory:
            job_repository = SQLiteJobRepository(f"{directory}/control-plane.db")
            service = _make_control_plane_service(job_repository=job_repository)
            stats_builder = StatsFixtureBuilder(service)
            read_service, _clock = _make_read_service(stats_builder=stats_builder)
            read_service.start()
            try:
                first = read_service.read_stats(0)
                self.assertTrue(first.warming_up)
                self.assertTrue(first.pending_refresh_event.wait(10))
                second = read_service.read_stats(0)
                self.assertFalse(second.warming_up)
                self.assertEqual(second.snapshot.payload["total"], 0)
                self.assertEqual(second.snapshot.payload["by_status"], {})
                self.assertEqual(len(second.snapshot.payload["by_day"]), 7)
                self.assertTrue(all(entry["total"] == 0 for entry in second.snapshot.payload["by_day"]))
            finally:
                read_service.stop()

    def test_failed_build_with_no_snapshot_stays_warming_without_fake_data(self):
        stats_builder = _ControlledBuilder(_stats_payload_factory)
        stats_builder.fail_after = 0
        service, _clock = _make_read_service(stats_builder=stats_builder)
        service.start()
        try:
            first = service.read_stats(0)
            self.assertTrue(first.warming_up)
            self.assertIsNotNone(first.pending_refresh_event)
            # The failed refresh still concludes its wait.
            self.assertTrue(first.pending_refresh_event.wait(10))
            # Bounded retry cadence: an immediate re-read is not a new build.
            second = service.read_stats(0)
            self.assertTrue(second.warming_up, "a failed build must never produce faked empty data")
            self.assertIsNone(second.snapshot)
            self.assertEqual(stats_builder.count(), 1)
            self.assertIsNone(second.pending_refresh_event, "failure retry cadence must not re-enqueue on every read")
        finally:
            _release_gate(stats_builder)
            service.stop()

    def test_target_builder_not_configured_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteDashboardReadSnapshotRepository(f"{directory}/control-plane.db")
            service, _clock = _make_read_service(
                snapshot_repository=repository,
                stats_builder=_ControlledBuilder(_stats_payload_factory),
            )
            with self.assertRaises(ValueError):
                service.read_targets()


class DashboardReadFreshnessTests(unittest.TestCase):
    def test_stale_read_serves_last_good_snapshot_and_refreshes_eventually(self):
        clock = _TestClock()
        stats_builder = _ControlledBuilder(_stats_payload_factory)
        service, clock = _make_read_service(stats_builder=stats_builder, interval=60.0, clock=clock)
        service.start()
        try:
            first = service.read_stats(0)
            self.assertTrue(first.warming_up)
            self.assertTrue(first.pending_refresh_event.wait(10))
            baseline = service.read_stats(0)
            self.assertEqual(baseline.snapshot.payload, {"total": 1, "by_status": {}, "by_day": []})
            generated_before = baseline.snapshot.generated_at

            # Make the snapshot stale for the next read: the read must still
            # return the last-good copy without waiting for the rebuild.
            clock.advance(61.0)
            stale_read = service.read_stats(0)
            self.assertFalse(stale_read.warming_up)
            self.assertEqual(stale_read.snapshot.payload, {"total": 1, "by_status": {}, "by_day": []})
            self.assertEqual(stale_read.snapshot.generated_at, generated_before)
            self.assertIsNotNone(stale_read.pending_refresh_event)

            # After the queued refresh concludes, the read shows new data.
            self.assertTrue(stale_read.pending_refresh_event.wait(10))
            refreshed = service.read_stats(0)
            self.assertEqual(refreshed.snapshot.payload, {"total": 2, "by_status": {}, "by_day": []})
            self.assertEqual(stats_builder.count(), 2)
        finally:
            _release_gate(stats_builder)
            service.stop()

    def test_responsiveness_while_builder_blocks_on_events_not_sleeps(self):
        stats_builder = _ControlledBuilder(_stats_payload_factory)
        service, clock = _make_read_service(stats_builder=stats_builder, interval=60.0)
        service.start()
        try:
            stats_builder.gate = threading.Event()  # first build blocks on the gate
            first = service.read_stats(0)
            self.assertTrue(first.warming_up)
            self.assertTrue(stats_builder.entered.wait(10))  # builder entered, blocked on gate
            blocked_read = service.read_stats(0)
            # The blocked build never extends the reader: same warming result,
            # coalesced on the one in-flight refresh.
            self.assertTrue(blocked_read.warming_up)
            self.assertEqual(blocked_read.pending_refresh_event, first.pending_refresh_event)
            stats_builder.gate.set()
            self.assertTrue(first.pending_refresh_event.wait(10))
            # Re-arm the gate so that the next (stale) rebuild blocks too, and
            # prove a last-good read happens while the builder is blocked.
            clock.advance(61.0)
            stats_builder.gate = threading.Event()
            while_build = service.read_stats(0)
            self.assertFalse(while_build.warming_up)
            self.assertEqual(while_build.snapshot.payload, {"total": 1, "by_status": {}, "by_day": []})
            self.assertIsNotNone(while_build.pending_refresh_event)
            stats_builder.gate.set()
            self.assertTrue(while_build.pending_refresh_event.wait(10))
            self.assertEqual(service.read_stats(0).snapshot.payload, {"total": 2, "by_status": {}, "by_day": []})
        finally:
            _release_gate(stats_builder)
            service.stop()

    def test_refresh_requests_coalesce_to_one_pending_or_inflight_per_key(self):
        stats_builder = _ControlledBuilder(_stats_payload_factory)
        service, _clock = _make_read_service(stats_builder=stats_builder, interval=60.0)
        stats_builder.gate = threading.Event()
        service.start()
        try:
            first = service.read_stats(0)
            self.assertTrue(first.warming_up)
            self.assertTrue(stats_builder.entered.wait(10))  # builder now in-flight for key ("stats", 0)
            # Two more viewers while the same build is in flight: coalesced.
            service.read_stats(0)
            service.read_stats(0)
            self.assertEqual(stats_builder.count(), 1, "concurrent reads must not start a second build")
            stats_builder.gate.set()
            self.assertTrue(first.pending_refresh_event.wait(10))
            # A fresh snapshot (age < interval) is not re-enqueued on read.
            settled = service.read_stats(0)
            self.assertIsNone(settled.pending_refresh_event)
            self.assertEqual(stats_builder.count(), 1)
        finally:
            _release_gate(stats_builder)
            service.stop()

    def test_refresh_failure_keeps_last_good_snapshot(self):
        clock = _TestClock()
        stats_builder = _ControlledBuilder(_stats_payload_factory)
        stats_builder.fail_after = 1
        service, clock = _make_read_service(stats_builder=stats_builder, interval=60.0, clock=clock)
        service.start()
        try:
            first = service.read_stats(0)
            self.assertTrue(first.pending_refresh_event.wait(10))
            good = service.read_stats(0)
            self.assertEqual(good.snapshot.payload, {"total": 1, "by_status": {}, "by_day": []})
            good_generated_at = good.snapshot.generated_at

            clock.advance(61.0)
            second = service.read_stats(0)  # stale -> queues a rebuild that will fail
            self.assertIsNotNone(second.pending_refresh_event)
            # Failure still completes the pending refresh wait.
            self.assertTrue(second.pending_refresh_event.wait(10))
            after_failure = service.read_stats(0)
            self.assertFalse(after_failure.warming_up)
            self.assertEqual(after_failure.snapshot.payload, {"total": 1, "by_status": {}, "by_day": []})
            self.assertEqual(after_failure.snapshot.generated_at, good_generated_at)
            # Bounded retry cadence: immediate reads after the failure do not
            # re-enqueue more builds.
            cadenced = service.read_stats(0)
            self.assertIsNone(cadenced.pending_refresh_event)
            self.assertEqual(stats_builder.count(), 2)
        finally:
            _release_gate(stats_builder)
            service.stop()

    def test_storage_save_failure_concludes_refresh_and_keeps_warming_without_data(self):
        stats_builder = _ControlledBuilder(_stats_payload_factory)
        repository = _FailingSaveRepository(InMemoryDashboardReadSnapshotRepository())
        repository.fail_saves = True
        service, clock = _make_read_service(snapshot_repository=repository, stats_builder=stats_builder, interval=60.0)
        service.start()
        try:
            first = service.read_stats(0)
            self.assertTrue(first.warming_up)
            self.assertTrue(first.pending_refresh_event.wait(10))  # failure concludes the wait
            with_no_saving = service.read_stats(0)
            self.assertTrue(with_no_saving.warming_up, "an unsaved snapshot must not be served")
            self.assertIsNone(with_no_saving.snapshot)
            self.assertIsNone(with_no_saving.pending_refresh_event, "retry cadence must not re-enqueue on every read")
            self.assertEqual(stats_builder.count(), 1)

            # After the cadence expires and storage recovers, key converges.
            repository.fail_saves = False
            clock.advance(61.0)
            retry = service.read_stats(0)
            self.assertIsNotNone(retry.pending_refresh_event)
            self.assertTrue(retry.pending_refresh_event.wait(10))
            served = service.read_stats(0)
            self.assertFalse(served.warming_up)
            # The retried build is the second builder invocation for this key.
            self.assertEqual(served.snapshot.payload, {"total": 2, "by_status": {}, "by_day": []})
        finally:
            _release_gate(stats_builder)
            service.stop()

    def test_storage_save_failure_with_prior_snapshot_keeps_last_good(self):
        clock = _TestClock()
        stats_builder = _ControlledBuilder(_stats_payload_factory)
        repository = _FailingSaveRepository(InMemoryDashboardReadSnapshotRepository())
        service, clock = _make_read_service(snapshot_repository=repository, stats_builder=stats_builder, interval=60.0, clock=clock)
        service.start()
        try:
            first = service.read_stats(0)
            self.assertTrue(first.pending_refresh_event.wait(10))
            good = service.read_stats(0)
            self.assertEqual(good.snapshot.payload, {"total": 1, "by_status": {}, "by_day": []})
            good_generated_at = good.snapshot.generated_at

            repository.fail_saves = True
            clock.advance(61.0)
            second = service.read_stats(0)  # stale -> build succeeds but save fails
            self.assertIsNotNone(second.pending_refresh_event)
            self.assertTrue(second.pending_refresh_event.wait(10))
            after_failure = service.read_stats(0)
            self.assertFalse(after_failure.warming_up)
            self.assertEqual(after_failure.snapshot.payload, {"total": 1, "by_status": {}, "by_day": []})
            self.assertEqual(after_failure.snapshot.generated_at, good_generated_at)
            self.assertIsNone(service.read_stats(0).pending_refresh_event, "failure retry cadence must be bounded")
        finally:
            _release_gate(stats_builder)
            service.stop()

    def test_periodic_refresh_only_active_loaded_keys(self):
        # Small interval so the periodic lane ticks within the event wait.
        calls = {}
        calls_lock = threading.Lock()
        second_build_done = threading.Event()

        def counting_stats_builder(tz_offset):
            with calls_lock:
                calls[tz_offset] = calls.get(tz_offset, 0) + 1
                count = calls[tz_offset]
            if tz_offset == 0 and count >= 2:
                second_build_done.set()
            return {"total": count, "by_status": {}, "by_day": []}

        # Real wall clock: the periodic lane must tick on its own interval.
        service, _clock = _make_read_service(stats_builder=counting_stats_builder, interval=0.05, clock=None)
        service.start()
        try:
            first = service.read_stats(0)
            self.assertTrue(first.pending_refresh_event.wait(10))
            # The loaded key is refreshed periodically without any new read.
            self.assertTrue(second_build_done.wait(10))
            # A never-read offset key was never built.
            with calls_lock:
                self.assertEqual(calls.get(5, 0), 0)
        finally:
            service.stop()


class DashboardReadTimezoneTests(unittest.TestCase):
    def test_tz_offset_bounds_validation_preserved(self):
        service, _clock = _make_read_service(stats_builder=_ControlledBuilder(_stats_payload_factory))
        for invalid in (841, -841):
            with self.assertRaises(ValueError):
                service.read_stats(invalid)
        # -840..840 remain accepted.
        for valid in (840, -840, 0):
            try:
                result = service.read_stats(valid)
            except ValueError:
                self.fail(f"tz_offset {valid} must remain valid")
            self.assertTrue(result.warming_up)

    def test_invalid_refresh_interval_is_refused(self):
        for invalid in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                DashboardReadService(
                    snapshot_repository=InMemoryDashboardReadSnapshotRepository(),
                    stats_builder=_ControlledBuilder(_stats_payload_factory),
                    refresh_interval_seconds=invalid,
                )

    def test_exact_minute_day_cut_matches_authoritative_stats_for_extreme_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            job_repository = SQLiteJobRepository(f"{directory}/control-plane.db")
            started_jobs = {
                # Just before/after UTC+5:30 (+330) day cut (18:30 UTC).
                "j-plus330-before": datetime(2026, 1, 15, 18, 25, 0),
                "j-plus330-after": datetime(2026, 1, 15, 18, 35, 0),
                # Just before/after UTC-5:00 (-300) day cut (05:00 UTC).
                "j-minus300-before": datetime(2026, 1, 15, 4, 55, 0),
                "j-minus300-after": datetime(2026, 1, 15, 5, 5, 0),
                "j-pending": None,
            }
            for index, (job_id, started_at) in enumerate(started_jobs.items()):
                job_repository.save(
                    JobRecord(
                        id=job_id,
                        worker_id="worker-a",
                        command="backup.run",
                        trigger="manual",
                        status="succeeded" if started_at is not None and index % 2 == 0 else (
                            "pending" if started_at is None else "failed"
                        ),
                        submitted_at=datetime(2026, 1, 15, 12, 0, 0),
                        started_at=started_at,
                        updated_at=datetime(2026, 1, 15, 12, 0, 0),
                    )
                )
            service = _make_control_plane_service(job_repository=job_repository)
            stats_builder = StatsFixtureBuilder(service)
            read_service, _clock = _make_read_service(stats_builder=stats_builder)
            read_service.start()
            try:
                for tz_offset in (0, 330, -300, 840, -840):
                    result = read_service.read_stats(tz_offset)
                    self.assertTrue(result.pending_refresh_event.wait(10))
                    served = read_service.read_stats(tz_offset)
                    self.assertFalse(served.warming_up)
                    self.assertEqual(
                        served.snapshot.payload,
                        service.job_stats(tz_offset_minutes=tz_offset),
                        f"read model must match authoritative stats for tz {tz_offset}",
                    )
            finally:
                read_service.stop()

    def test_exact_day_cut_with_fixed_now_matches_expected_by_day(self):
        with tempfile.TemporaryDirectory() as directory:
            job_repository = SQLiteJobRepository(f"{directory}/control-plane.db")
            # started_at 2026-01-15T04:30:00 with tz -300 (cut 05:00 UTC) goes
            # to local day 2026-01-14; with tz +330 (cut 18:30 UTC) it is day
            # 2026-01-15. 45 minutes of slack on both sides of the boundary.
            crossings = {
                "tz0-day": (0, "2026-01-15"),
                "tz-minus300-day": (-300, "2026-01-14"),
                "tz-plus330-day": (330, "2026-01-15"),
            }
            job_repository.save(
                JobRecord(
                    id="boundary-job",
                    worker_id="worker-a",
                    command="backup.run",
                    trigger="manual",
                    status="succeeded",
                    submitted_at=datetime(2026, 1, 15, 4, 30, 0),
                    started_at=datetime(2026, 1, 15, 4, 30, 0),
                    updated_at=datetime(2026, 1, 15, 4, 30, 0),
                )
            )
            service = _make_control_plane_service(job_repository=job_repository)
            clock = _TestClock(start=datetime(2026, 1, 15, 12, 0, 0))
            stats_builder = StatsFixtureBuilder(service, fixed_now=clock)
            read_service, _clock = _make_read_service(stats_builder=stats_builder, clock=clock)
            read_service.start()
            try:
                for tz_offset, expected_day in crossings.values():
                    result = read_service.read_stats(tz_offset)
                    self.assertTrue(result.pending_refresh_event.wait(10))
                    payload = read_service.read_stats(tz_offset).snapshot.payload
                    expected = service.job_repository.job_stats(tz_offset_minutes=tz_offset, now=clock())
                    self.assertEqual(payload, expected)
                    day_entry = next(entry for entry in payload["by_day"] if entry["total"])
                    self.assertEqual(day_entry["date"], expected_day)
            finally:
                read_service.stop()


class DashboardReadSnapshotStoreTests(unittest.TestCase):
    def test_lru_bounds_distinct_tz_offset_keys(self):
        stats_builder = _ControlledBuilder(lambda tz: {"total": tz, "by_status": {}, "by_day": []})
        service, _clock = _make_read_service(stats_builder=stats_builder, max_tz_offset_keys=2)
        service.start()
        try:
            for tz_offset in (-840, 840, 1):
                result = service.read_stats(tz_offset)
                self.assertTrue(result.pending_refresh_event.wait(10), f"offset {tz_offset} build never finished")
            # The oldest offset was evicted from the bounded key set.
            self.assertIsNone(service.current_snapshot("stats", -840))
            self.assertIsNotNone(service.current_snapshot("stats", 840))
            self.assertIsNotNone(service.current_snapshot("stats", 1))
            # Re-reading an evicted offset rebuilds it (bounded work, not leakage).
            result = service.read_stats(-840)
            self.assertTrue(result.pending_refresh_event.wait(10))
            self.assertIsNotNone(service.current_snapshot("stats", -840))
            self.assertEqual(stats_builder.count(), 4)
        finally:
            _release_gate(stats_builder)
            service.stop()

    def test_pending_lru_eviction_prunes_queue_without_losing_other_work(self):
        stats_builder = _ControlledBuilder(lambda tz: {"total": tz, "by_status": {}, "by_day": []})
        service, _clock = _make_read_service(stats_builder=stats_builder, max_tz_offset_keys=2, interval=60.0)
        service.start()
        try:
            stats_builder.gate = threading.Event()
            in_flight = service.read_stats(100)
            self.assertTrue(stats_builder.entered.wait(10))  # worker is building offset 100
            queued_200 = service.read_stats(200)  # queued behind the in-flight build
            queued_300 = service.read_stats(300)  # evicts 100 from the LRU
            queued_400 = service.read_stats(400)  # evicts 200 (still queued, never built)

            stats_builder.gate.set()
            self.assertTrue(in_flight.pending_refresh_event.wait(10))  # build concludes, result dropped below

            # Both queued keys drain (the queue is not lost by pruned entries).
            self.assertTrue(queued_300.pending_refresh_event.wait(10))
            self.assertTrue(queued_400.pending_refresh_event.wait(10))
            self.assertIsNotNone(service.current_snapshot("stats", 300))
            self.assertIsNotNone(service.current_snapshot("stats", 400))
            # Evicted keys were not published: 100 was in-flight when evicted,
            # 200 was queued; their queue entries are pruned, not executed.
            self.assertIsNone(service.current_snapshot("stats", 100))
            self.assertIsNone(service.current_snapshot("stats", 200))
            self.assertEqual(stats_builder.count(), 3)  # 100, 300, 400 only
        finally:
            _release_gate(stats_builder)
            service.stop()

    def test_evicted_inflight_key_is_not_republished_and_readd_rebuilds(self):
        stats_builder = _ControlledBuilder(lambda tz: {"total": tz, "by_status": {}, "by_day": []})
        service, _clock = _make_read_service(stats_builder=stats_builder, max_tz_offset_keys=2, interval=60.0)
        service.start()
        try:
            stats_builder.gate = threading.Event()
            in_flight = service.read_stats(100)
            self.assertTrue(stats_builder.entered.wait(10))  # build in flight for 100
            service.read_stats(200)
            evictor = service.read_stats(300)  # evicts 100 mid-build

            stats_builder.gate.set()
            # The mid-build refresh still concludes its waiter...
            self.assertTrue(in_flight.pending_refresh_event.wait(10))
            # ... the evicted state is never resurrected by the old build...
            self.assertIsNone(service.current_snapshot("stats", 100))
            # ... and the non-evicted queued key (300) drains normally.
            self.assertTrue(evictor.pending_refresh_event.wait(10))
            self.assertIsNotNone(service.current_snapshot("stats", 300))

            # Re-adding the offset creates a fresh state that rebuilds.
            readded = service.read_stats(100)
            self.assertTrue(evictor.pending_refresh_event.wait(10))
            self.assertTrue(readded.pending_refresh_event.wait(10))
            self.assertIsNotNone(service.current_snapshot("stats", 100))
            self.assertEqual(
                [call[0] for call in stats_builder.calls].count(100),
                2,
                "re-added offset keys must rebuild once for the new state",
            )
        finally:
            _release_gate(stats_builder)
            service.stop()

    def test_persisted_snapshots_hydrate_on_restart_without_rebuilding(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteDashboardReadSnapshotRepository(f"{directory}/control-plane.db")
            stats_builder = _ControlledBuilder(_stats_payload_factory)
            service, _clock = _make_read_service(
                snapshot_repository=repository,
                stats_builder=stats_builder,
            )
            service.start()
            try:
                first = service.read_stats(0)
                self.assertTrue(first.warming_up)
                self.assertTrue(first.pending_refresh_event.wait(10))
                self.assertEqual(stats_builder.count(), 1)
            finally:
                service.stop()
            persisted = repository.load_all()
            self.assertEqual(len(persisted), 1)
            self.assertEqual(persisted[0].payload, {"total": 1, "by_status": {}, "by_day": []})

            # Restart: the hydrated snapshot is served without a single build.
            restarted, _clock = _make_read_service(
                snapshot_repository=repository,
                stats_builder=stats_builder,
            )
            restarted.start()
            try:
                warmed = restarted.read_stats(0)
                self.assertFalse(warmed.warming_up)
                self.assertEqual(warmed.snapshot.payload, {"total": 1, "by_status": {}, "by_day": []})
                self.assertEqual(stats_builder.count(), 1, "restart hydration must not rebuild")
            finally:
                restarted.stop()

    def test_targets_snapshot_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteDashboardReadSnapshotRepository(f"{directory}/control-plane.db")
            targets_builder = _ControlledBuilder(_targets_payload_factory)
            service, _clock = _make_read_service(
                snapshot_repository=repository,
                stats_builder=_ControlledBuilder(_stats_payload_factory),
                targets_builder=targets_builder,
            )
            service.start()
            try:
                first = service.read_targets()
                self.assertTrue(first.warming_up)
                self.assertTrue(first.pending_refresh_event.wait(10))
                second = service.read_targets()
                self.assertFalse(second.warming_up)
                self.assertEqual(second.snapshot.payload, {"items": [{"name": "target-1"}]})
            finally:
                service.stop()

            restarted, _clock = _make_read_service(
                snapshot_repository=repository,
                stats_builder=_ControlledBuilder(_stats_payload_factory),
                targets_builder=_ControlledBuilder(_targets_payload_factory),
            )
            restarted.start()
            try:
                served = restarted.read_targets()
                self.assertFalse(served.warming_up)
                self.assertEqual(served.snapshot.payload, {"items": [{"name": "target-1"}]})
            finally:
                restarted.stop()

    def test_corrupt_persisted_row_is_skipped_and_invalid_offsets_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteDashboardReadSnapshotRepository(f"{directory}/control-plane.db")
            stats_builder = _ControlledBuilder(_stats_payload_factory)
            service, _clock = _make_read_service(
                snapshot_repository=repository,
                stats_builder=stats_builder,
            )
            service.start()
            try:
                result = service.read_stats(0)
                self.assertTrue(result.pending_refresh_event.wait(10))
            finally:
                service.stop()

            # Corrupt one payload and inject an out-of-range offset row.
            connection = sqlite3.connect(f"{directory}/control-plane.db")
            try:
                connection.execute("UPDATE dashboard_read_snapshots SET payload_json = '{not json' WHERE storage_key = 'stats:0'")
                connection.execute(
                    "INSERT OR REPLACE INTO dashboard_read_snapshots"
                    " (storage_key, kind, tz_offset_minutes, payload_json, generated_at)"
                    " VALUES ('stats:900', 'stats', 900, '{}', '2026-01-15T12:00:00')"
                )
                connection.commit()
            finally:
                connection.close()

            restarted, _clock = _make_read_service(
                snapshot_repository=repository,
                stats_builder=stats_builder,
            )
            restarted.start()
            try:
                result = restarted.read_stats(0)
                self.assertTrue(
                    result.warming_up,
                    "a corrupt persisted row must not hydrate a broken snapshot",
                )
                self.assertTrue(result.pending_refresh_event.wait(10))
                served = restarted.read_stats(0)
                self.assertIsNotNone(served.snapshot)
                self.assertIsNotNone(restarted.current_snapshot("stats", 0))
            finally:
                restarted.stop()


class HarnessAuth:
    def parse_session_token(self, token):
        roles = {"viewer-token": "viewer", "admin-token": "admin"}
        role = roles.get(token)
        return {"username": "user-a", "role": role} if role else None

    @staticmethod
    def can_access(role, required_role):
        return role == "admin" or (role == "viewer" and required_role == "viewer")


class _BaseHttpHarness(unittest.TestCase):
    def setUp(self):
        self.root_directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.root_directory.cleanup()

    def _snapshot_repository(self):
        return SQLiteDashboardReadSnapshotRepository(f"{self.root_directory.name}/control-plane.db")

    def _build_app(self, service, read_service):
        application = ControlPlaneApplication(
            auth_service=HarnessAuth(),
            control_plane_service=service,
            dashboard_read_service=read_service,
        )
        server = ControlPlaneHTTPServer(("127.0.0.1", 0), ControlPlaneRequestHandler, application=application)
        host, port = server.server_address
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # LIFO cleanups: shutdown, close, then join with a bounded timeout.
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://{host}:{port}"

    def _request(self, base_url, path, token="viewer-token", method="GET"):
        headers = {}
        if token:
            headers["Cookie"] = f"cp_session={token}"
        request = Request(base_url + path, headers=headers, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            body = error.read()
            error.close()
            return error.code, error.headers, body

    def _json(self, _status, _headers, body):
        return json.loads(body.decode("utf-8"))

    def _persist_stats_snapshot(self, repository, service, tz_offset=0):
        payload = service.job_stats(tz_offset_minutes=tz_offset)
        repository.save(
            DashboardReadSnapshotRecord(
                kind="stats", tz_offset_minutes=tz_offset, payload=payload, generated_at=utcnow()
            )
        )
        return payload

    def _persist_targets_snapshot(self, repository, service):
        payload = {"items": [build_target_view(item, service) for item in service.list_targets()]}
        repository.save(
            DashboardReadSnapshotRecord(kind="targets", payload=payload, generated_at=utcnow())
        )
        return payload


class DashboardReadModelHttpTests(_BaseHttpHarness):
    def test_stats_projection_serves_cached_body_and_freshness_headers(self):
        service = _make_control_plane_service()
        repository = self._snapshot_repository()
        expected_body = self._persist_stats_snapshot(repository, service, tz_offset=0)
        # Hydration happens at construction; no worker lane needed for reads.
        read_service, _clock = _make_read_service(
            snapshot_repository=repository,
            stats_builder=StatsFixtureBuilder(service),
            targets_builder=lambda: {"items": []},
            interval=5.0,
        )
        read_service.start()
        try:
            base_url = self._build_app(service, read_service)
            for method, expect_body in (("GET", True), ("HEAD", False)):
                status, headers, body = self._request(base_url, "/api/v1/jobs/stats?tz_offset=0", method=method)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
                updated_at = headers["X-Read-Model-Updated-At"]
                self.assertTrue(updated_at.endswith("Z"))
                age = int(headers["X-Read-Model-Age-Seconds"])
                self.assertGreaterEqual(age, 0)
                snapshot = read_service.current_snapshot("stats", 0)
                self.assertEqual(updated_at, snapshot.generated_at.isoformat() + "Z")
                if expect_body:
                    served = json.loads(body.decode("utf-8"))
                    self.assertEqual(served, expected_body)
                    self.assertEqual(
                        served,
                        service.job_stats(tz_offset_minutes=0),
                        "cached body must equal the legacy {total, by_status, by_day} shape",
                    )
                    self.assertEqual(expected_body["by_status"], served["by_status"])
                    self.assertEqual(len(served["by_day"]), len(expected_body["by_day"]))
                else:
                    self.assertEqual(body, b"")
        finally:
            read_service.stop()

    def test_stats_warmup_returns_503_with_retry_after_and_code_without_fake_data(self):
        service = _make_control_plane_service()
        repository = self._snapshot_repository()
        stats_builder = _ControlledBuilder(_stats_payload_factory)
        stats_builder.gate = threading.Event()  # build never completes during the test
        read_service, _clock = _make_read_service(
            snapshot_repository=repository,
            stats_builder=stats_builder,
            interval=5.0,
        )
        read_service.start()
        try:
            base_url = self._build_app(service, read_service)
            status, headers, body = self._request(base_url, "/api/v1/jobs/stats?tz_offset=-300")
            self.assertEqual(status, 503)
            payload = self._json(status, headers, body)
            self.assertEqual(payload, {"error": "dashboard read model is warming up", "code": "read_model_warming_up"})
            self.assertEqual(payload["code"], READ_MODEL_WARMUP_CODE)
            self.assertEqual(headers["Retry-After"], "5")
            self.assertEqual(headers["Cache-Control"], "no-store")
        finally:
            # Release the blocked builder before stopping the lane (bounded).
            _release_gate(stats_builder)
            read_service.stop()

    def test_targets_uses_projection_only_while_mutation_and_detail_stay_live(self):
        service = _make_control_plane_service()
        service.register_target("target-a", "worker-a")
        repository = self._snapshot_repository()
        expected_items = self._persist_targets_snapshot(repository, service)
        read_service, _clock = _make_read_service(
            snapshot_repository=repository,
            stats_builder=StatsFixtureBuilder(service),
            targets_builder=lambda: {
                "items": [build_target_view(item, service) for item in service.list_targets()]
            },
        )
        base_url = self._build_app(service, read_service)
        status, headers, body = self._request(base_url, "/api/v1/targets")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("X-Read-Model-Updated-At", dict(headers))
        self.assertIn("X-Read-Model-Age-Seconds", dict(headers))
        served = self._json(status, headers, body)
        self.assertEqual(
            served,
            expected_items,
            "projected item fields must equal the legacy target view fields",
        )

        # Mutation stays live and does not serve through the projection.
        post_headers = {
            "Cookie": "cp_session=admin-token",
            "Content-Type": "application/json",
        }
        request = Request(
            base_url + "/api/v1/targets",
            data=json.dumps({"name": "target-b", "worker_id": "worker-a"}).encode("utf-8"),
            headers=post_headers,
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            created = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 201)
        # The cached list keeps its last-good content until refreshed.
        still_cached = self._json(*self._request(base_url, "/api/v1/targets"))
        self.assertEqual([item["name"] for item in still_cached["items"]], ["target-a"])
        # Detail reads stay live against authoritative state.
        status, headers, body = self._request(base_url, f"/api/v1/targets/{created['id']}/validate")
        self.assertEqual(status, 200)
        self.assertEqual(self._json(status, headers, body)["target_id"], created["id"])
        self.assertNotIn("X-Read-Model-Updated-At", dict(headers))

    def test_targets_warmup_uses_projection_route(self):
        service = _make_control_plane_service()
        service.register_target("target-a", "worker-a")
        repository = self._snapshot_repository()
        read_service, _clock = _make_read_service(
            snapshot_repository=repository,
            stats_builder=StatsFixtureBuilder(service),
            targets_builder=lambda: {"items": []},
        )
        base_url = self._build_app(service, read_service)
        status, headers, body = self._request(base_url, "/api/v1/targets")
        self.assertEqual(status, 503)  # no snapshot yet: explicit warm-up, no legacy fallback
        self.assertEqual(self._json(status, headers, body)["code"], READ_MODEL_WARMUP_CODE)

    def test_legacy_app_without_read_model_configured_keeps_direct_behavior(self):
        service = _make_control_plane_service()
        base_url = self._build_app(service, None)
        status, headers, body = self._request(base_url, "/api/v1/jobs/stats?tz_offset=0")
        self.assertEqual(status, 200)
        self.assertNotIn("X-Read-Model-Updated-At", dict(headers))
        self._json(status, headers, body)  # legacy direct body still parses
        status, headers, body = self._request(base_url, "/api/v1/targets")
        self.assertEqual(status, 200)
        self.assertNotIn("X-Read-Model-Updated-At", dict(headers))

    def test_unauthenticated_requests_are_rejected_before_the_projection(self):
        service = _make_control_plane_service()
        read_service, _clock = _make_read_service(
            snapshot_repository=self._snapshot_repository(),
            stats_builder=StatsFixtureBuilder(service),
            targets_builder=lambda: {"items": []},
        )
        base_url = self._build_app(service, read_service)
        status, _headers, body = self._request(base_url, "/api/v1/jobs/stats", token=None)
        self.assertEqual(status, 401)
        status, _headers, body = self._request(base_url, "/api/v1/targets", token=None)
        self.assertEqual(status, 401)


class MainLifecyclePublicSmokeTests(_BaseHttpHarness):
    def test_lifecycle_starts_and_primes_lane_and_stops_it_in_finally(self):
        service = _make_control_plane_service()
        service.register_target("target-a", "worker-a")
        read_service, _clock = _make_read_service(
            stats_builder=StatsFixtureBuilder(service),
            targets_builder=lambda: {
                "items": [build_target_view(item, service) for item in service.list_targets()]
            },
        )
        scheduler = SchedulerService(service, timezone_name="UTC")
        application = ControlPlaneApplication(
            auth_service=HarnessAuth(),
            control_plane_service=service,
            dashboard_read_service=read_service,
            scheduler=scheduler,
        )
        ready = threading.Event()
        server_thread = threading.Thread(
            target=run_control_plane, args=(application, "127.0.0.1", 0, ready), daemon=True
        )
        server_thread.start()
        try:
            self.assertTrue(ready.wait(10), "the control plane did not become ready")
            self.addCleanup(scheduler.stop)
            self.addCleanup(read_service.stop)
            self.assertTrue(read_service.is_running())
            # Priming is asynchronous: reads answer immediately, without any
            # synchronous fallback build (warm-up or cached content).
            status, headers, body = self._request(
                f"http://127.0.0.1:{application.server.server_address[1]}",
                "/api/v1/jobs/stats?tz_offset=0",
            )
            self.assertIn(status, (200, 503))
            if status == 503:
                self.assertEqual(self._json(status, headers, body)["code"], READ_MODEL_WARMUP_CODE)
            # Health endpoint still works.
            status, _headers, _body = self._request(
                f"http://127.0.0.1:{application.server.server_address[1]}", "/healthz"
            )
            self.assertEqual(status, 200)
        finally:
            server = getattr(application, "server", None)
            if server is not None:
                server.shutdown()
        server_thread.join(10)
        self.assertFalse(server_thread.is_alive())
        # The finally block in run_control_plane stopped the maintenance lane.
        self.assertFalse(read_service.is_running())


if __name__ == "__main__":
    unittest.main()