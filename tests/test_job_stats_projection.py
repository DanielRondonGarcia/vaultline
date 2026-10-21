"""Compact job-stats projection tests (dashboard-read-models T1, spec S1-S2).

The dashboard aggregates were served directly from the wide ``jobs`` table
(one row per job with payload/log columns). This module pins the new compact
``job_stats_facts`` projection (job_id, normalized status, started_at only):

- the projection exists and stays in sync with every job mutation path,
- existing history is backfilled additively exactly once per database,
- totals are never duplicated after reopen or re-initialization,
- status transitions reach the projection even when ``updated_at`` is equal
  (which rules out any updated_at-cursor refresh design),
- claim/expired-lease repository mutations keep the projection true,

and it keeps the regression behavior of the original aggregate: the exact
seven-day/minute tz-offset day grouping, the null ``started_at`` handling and
the ``cancelled`` -> ``canceled`` normalization.

Projection-integrity tests intentionally fail on the pre-projection code
(no ``job_stats_facts`` table), which is the RED evidence for this task;
the behavioral regressions pass on the original aggregate as well.
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from src.control_plane.domain.models import JobRecord
from src.control_plane.infrastructure.repositories.sqlite import SQLiteJobRepository

JOB_STATS_FACTS_TABLE = "job_stats_facts"
JOB_STATS_BACKFILL_META_KEY = "job_stats_facts_backfill_done"

# Fixed "now" so the seven-day keys are deterministic: 2026-01-09..2026-01-15.
_STATS_NOW = datetime(2026, 1, 15, 12, 0, 0)

# Job started 45 minutes before the local (-300 minutes) day boundary of
# 2026-01-15 00:00, so tz 0 groups it on 2026-01-15 and tz -300 on 2026-01-14.
_BOUNDARY_STARTED_AT = datetime(2026, 1, 15, 4, 30, 0)

_TZ0_EXPECTED_STATS = {
    "total": 5,
    "by_status": {"canceled": 1, "failed": 1, "pending": 1, "succeeded": 2},
    "by_day": [
        {"date": "2026-01-09", "total": 1, "success": 0, "failed": 0},
        {"date": "2026-01-10", "total": 0, "success": 0, "failed": 0},
        {"date": "2026-01-11", "total": 0, "success": 0, "failed": 0},
        {"date": "2026-01-12", "total": 1, "success": 0, "failed": 1},
        {"date": "2026-01-13", "total": 0, "success": 0, "failed": 0},
        {"date": "2026-01-14", "total": 0, "success": 0, "failed": 0},
        {"date": "2026-01-15", "total": 2, "success": 2, "failed": 0},
    ],
}

_TZ_MINUS_300_EXPECTED_STATS = {
    "total": 5,
    "by_status": {"canceled": 1, "failed": 1, "pending": 1, "succeeded": 2},
    "by_day": [
        {"date": "2026-01-09", "total": 1, "success": 0, "failed": 0},
        {"date": "2026-01-10", "total": 0, "success": 0, "failed": 0},
        {"date": "2026-01-11", "total": 0, "success": 0, "failed": 0},
        {"date": "2026-01-12", "total": 1, "success": 0, "failed": 1},
        {"date": "2026-01-13", "total": 0, "success": 0, "failed": 0},
        {"date": "2026-01-14", "total": 2, "success": 2, "failed": 0},
        {"date": "2026-01-15", "total": 0, "success": 0, "failed": 0},
    ],
}


def _fixture_jobs() -> list:
    return [
        JobRecord(
            id="p1-succeeded",
            worker_id="worker-a",
            command="backup.run",
            trigger="manual",
            status="succeeded",
            submitted_at=datetime(2026, 1, 15, 1, 0, 0),
            started_at=datetime(2026, 1, 15, 1, 0, 0),
            finished_at=datetime(2026, 1, 15, 1, 5, 0),
            updated_at=datetime(2026, 1, 15, 1, 5, 0),
        ),
        # 45 minutes before the -300 tz day boundary of Jan 15.
        JobRecord(
            id="p2-success-crosses-tz",
            worker_id="worker-a",
            command="backup.run",
            trigger="schedule",
            status="succeeded",
            submitted_at=_BOUNDARY_STARTED_AT,
            started_at=_BOUNDARY_STARTED_AT,
            updated_at=_BOUNDARY_STARTED_AT,
        ),
        JobRecord(
            id="p3-failed",
            worker_id="worker-a",
            command="restore.run",
            trigger="manual",
            status="failed",
            submitted_at=datetime(2026, 1, 12, 23, 30, 0),
            started_at=datetime(2026, 1, 12, 23, 30, 0),
            updated_at=datetime(2026, 1, 12, 23, 30, 0),
        ),
        # No started_at: pending. Must count in total/by_status but no day.
        JobRecord(
            id="p4-pending",
            worker_id="worker-a",
            command="backup.run",
            trigger="schedule",
            status="pending",
            submitted_at=datetime(2026, 1, 14, 6, 0, 0),
            updated_at=datetime(2026, 1, 14, 6, 0, 0),
        ),
        # Legacy raw spelling stored via the public save seam must be
        # normalized to "canceled" in the aggregate and in the projection.
        JobRecord(
            id="p5-cancelled",
            worker_id="worker-a",
            command="backup.run",
            trigger="automatic",
            status="cancelled",
            submitted_at=datetime(2026, 1, 9, 5, 0, 0),
            started_at=datetime(2026, 1, 9, 5, 0, 0),
            updated_at=datetime(2026, 1, 9, 5, 0, 0),
        ),
    ]


def _save_fixture(repository: SQLiteJobRepository) -> None:
    for job in _fixture_jobs():
        repository.save(job)


def _recomputed_stats(jobs: list, tz_offset_minutes: int, now: datetime) -> dict:
    """Mirror of the documented aggregate semantics, recomputed from the
    public listing, used as an independent expectation (no SQL involved)."""
    offset = timedelta(minutes=tz_offset_minutes)
    today = (now + offset).date()
    day_keys = [(today - timedelta(days=index)).isoformat() for index in range(6, -1, -1)]
    window_start = datetime.fromisoformat(day_keys[0]) - offset
    status_counts = {}
    day_status_counts = {}
    for job in jobs:
        status = "canceled" if job.status == "cancelled" else job.status
        status_counts[status] = status_counts.get(status, 0) + 1
        if job.started_at is not None and job.started_at >= window_start:
            day = (job.started_at + offset).date().isoformat()
            day_status_counts[(day, status)] = day_status_counts.get((day, status), 0) + 1
    by_day = []
    for day in day_keys:
        counts = {s: c for (d, s), c in day_status_counts.items() if d == day}
        by_day.append(
            {
                "date": day,
                "total": sum(counts.values()),
                "success": counts.get("succeeded", 0),
                "failed": counts.get("failed", 0),
            }
        )
    return {"total": len(jobs), "by_status": dict(sorted(status_counts.items())), "by_day": by_day}


class JobStatsProjectionTests(unittest.TestCase):
    @staticmethod
    def _facts_rows(database_path: str) -> list:
        rows = []
        try:
            connection = sqlite3.connect(database_path, timeout=5)
            try:
                rows = connection.execute(
                    "SELECT job_id, status, started_at FROM job_stats_facts ORDER BY job_id"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.OperationalError:
            return []
        return [(row[0], row[1], row[2]) for row in rows]

    def test_compact_projection_exists_and_tracks_saved_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database_path)
            _save_fixture(repository)
            facts = self._facts_rows(database_path)
            self.assertEqual(
                [(fact[0], fact[1]) for fact in facts],
                [
                    ("p1-succeeded", "succeeded"),
                    ("p2-success-crosses-tz", "succeeded"),
                    ("p3-failed", "failed"),
                    ("p4-pending", "pending"),
                    ("p5-cancelled", "canceled"),
                ],
            )
            self.assertEqual(
                [fact[2] for fact in facts],
                [
                    "2026-01-15T01:00:00",
                    "2026-01-15T04:30:00",
                    "2026-01-12T23:30:00",
                    None,
                    "2026-01-09T05:00:00",
                ],
            )
            self.assertEqual(
                repository.job_stats(tz_offset_minutes=0, now=_STATS_NOW),
                _TZ0_EXPECTED_STATS,
            )
            self.assertEqual(
                repository.job_stats(tz_offset_minutes=-300, now=_STATS_NOW),
                _TZ_MINUS_300_EXPECTED_STATS,
            )

    def test_status_transition_with_equal_updated_at_reaches_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database_path)
            original = _fixture_jobs()[0]
            repository.save(original)
            # Same updated_at (the exact failure mode of an updated_at-cursor
            # design) with a new terminal status.
            transitioned = JobRecord(
                id=original.id,
                worker_id=original.worker_id,
                command=original.command,
                trigger=original.trigger,
                status="failed",
                submitted_at=original.submitted_at,
                started_at=original.started_at,
                finished_at=original.updated_at,
                updated_at=original.updated_at,
            )
            repository.save(transitioned)
            facts = self._facts_rows(database_path)
            self.assertEqual(facts, [("p1-succeeded", "failed", "2026-01-15T01:00:00")])
            stats = repository.job_stats(tz_offset_minutes=0, now=_STATS_NOW)
            self.assertEqual(stats["total"], 1)
            self.assertEqual(stats["by_status"], {"failed": 1})

    def test_backfill_from_existing_history_runs_once(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = f"{directory}/control-plane.db"
            # Simulate a pre-projection database: the wide jobs table carries
            # existing history without any projection table or marker.
            connection = sqlite3.connect(database_path, timeout=5)
            try:
                connection.executescript(
                    """
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
                    CREATE TABLE IF NOT EXISTS schema_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    """
                )
                connection.commit()
            finally:
                connection.close()
            repository = SQLiteJobRepository(database_path)
            _save_fixture(repository)
            first_stats = repository.job_stats(tz_offset_minutes=0, now=_STATS_NOW)
            self.assertEqual(first_stats["total"], 5)

            # Force a legacy-shaped database again: drop the projection and
            # its one-time marker, keep the job history.
            connection = sqlite3.connect(database_path, timeout=5)
            try:
                connection.execute("DROP TABLE IF EXISTS job_stats_facts")
                connection.execute(
                    "DELETE FROM schema_meta WHERE key = ?",
                    (JOB_STATS_BACKFILL_META_KEY,),
                )
                connection.commit()
            finally:
                connection.close()

            reopened = SQLiteJobRepository(database_path)
            self.assertEqual(
                reopened.job_stats(tz_offset_minutes=0, now=_STATS_NOW),
                first_stats,
            )
            reopened_stats = reopened.job_stats(tz_offset_minutes=0, now=_STATS_NOW)
            self.assertEqual(reopened_stats["total"], 5)
            self.assertEqual(sum(reopened_stats["by_status"].values()), 5)
            connection = sqlite3.connect(database_path, timeout=5)
            try:
                markers = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = ?",
                    (JOB_STATS_BACKFILL_META_KEY,),
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(len(markers), 1)

            # Mutations continue to maintain the projection after backfill.
            reopened.save(
                JobRecord(
                    id="p6-after-backfill",
                    worker_id="worker-a",
                    command="backup.run",
                    trigger="manual",
                    status="succeeded",
                    submitted_at=datetime(2026, 1, 15, 5, 0, 0),
                    started_at=datetime(2026, 1, 15, 5, 0, 0),
                    updated_at=datetime(2026, 1, 15, 5, 0, 0),
                )
            )
            final_stats = reopened.job_stats(tz_offset_minutes=0, now=_STATS_NOW)
            self.assertEqual(final_stats["total"], 6)
            self.assertEqual(sum(final_stats["by_status"].values()), 6)

    def test_reopen_initialization_never_duplicates_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database_path)
            _save_fixture(repository)
            for _reopening in range(3):
                SQLiteJobRepository(database_path)
            reopened = SQLiteJobRepository(database_path)
            stats = reopened.job_stats(tz_offset_minutes=0, now=_STATS_NOW)
            self.assertEqual(stats["total"], 5)
            self.assertEqual(sum(stats["by_status"].values()), 5)
            facts = self._facts_rows(database_path)
            self.assertEqual(len(facts), 5)

    def test_claim_and_expired_lease_mutations_track_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database_path)
            submitted_at = datetime(2026, 1, 15, 1, 0, 0)
            repository.save(
                JobRecord(
                    id="p7-lease-cycle",
                    worker_id="worker-a",
                    command="backup.run",
                    trigger="manual",
                    status="pending",
                    submitted_at=submitted_at,
                    updated_at=submitted_at,
                )
            )
            claimed = repository.claim_pending_for_worker("worker-a")
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0].status, "in_progress")
            claimed_started_at = claimed[0].started_at.isoformat()
            running_stats = repository.job_stats(tz_offset_minutes=0, now=_STATS_NOW)
            self.assertEqual(running_stats["total"], 1)
            self.assertEqual(running_stats["by_status"], {"in_progress": 1})
            self.assertEqual(
                self._facts_rows(database_path),
                [("p7-lease-cycle", "in_progress", claimed_started_at)],
            )

            # Lease-field maintenance (renew-like) must not corrupt the
            # projection because it never changes status or started_at.
            connection = sqlite3.connect(database_path, timeout=5)
            try:
                connection.execute(
                    "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
                    ("2026-01-15T00:00:00", "p7-lease-cycle"),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(
                self._facts_rows(database_path),
                [("p7-lease-cycle", "in_progress", claimed_started_at)],
            )

            reconciled = repository.reconcile_expired_leases()
            self.assertEqual(reconciled, 1)
            expired_stats = repository.job_stats(tz_offset_minutes=0, now=_STATS_NOW)
            self.assertEqual(expired_stats["total"], 1)
            self.assertEqual(expired_stats["by_status"], {"failed": 1})
            # Reconciliation keeps the claim-time started_at, which in this
            # fixture is the current date, outside the frozen seven-day
            # window of _STATS_NOW's day keys: it counts in totals only.
            self.assertEqual(
                self._facts_rows(database_path),
                [("p7-lease-cycle", "failed", claimed_started_at)],
            )

    def test_empty_database_report_zeroed_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteJobRepository(f"{directory}/control-plane.db")
            stats = repository.job_stats(tz_offset_minutes=-300, now=_STATS_NOW)
            self.assertEqual(stats["total"], 0)
            self.assertEqual(stats["by_status"], {})

    def test_projection_matches_public_jobs_aggregate_after_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteJobRepository(f"{directory}/control-plane.db")
            base = datetime(2026, 1, 11, 0, 0, 0)
            statuses = ["succeeded", "failed", "pending", "cancelled"]
            for index in range(24):
                started = base + timedelta(hours=index, minutes=index)
                repository.save(
                    JobRecord(
                        id=f"cycle-{index:02d}",
                        worker_id="worker-a",
                        command="backup.run",
                        trigger="schedule",
                        status=statuses[index % len(statuses)],
                        submitted_at=started,
                        started_at=started if index % 4 != 2 else None,
                        updated_at=started,
                    )
                )
            for index in range(0, 24, 2):
                # Retransmit transitions with equal updated_at values.
                original = repository.get(f"cycle-{index:02d}")
                repository.save(
                    JobRecord(
                        id=original.id,
                        worker_id=original.worker_id,
                        command=original.command,
                        trigger=original.trigger,
                        status="failed" if original.status != "failed" else "succeeded",
                        submitted_at=original.submitted_at,
                        started_at=original.started_at,
                        updated_at=original.updated_at,
                    )
                )
            stored = repository.list()
            now = _STATS_NOW
            for tz_offset in (0, -300, 120):
                self.assertEqual(
                    repository.job_stats(tz_offset_minutes=tz_offset, now=now),
                    _recomputed_stats(stored, tz_offset, now),
                )


if __name__ == "__main__":
    unittest.main()