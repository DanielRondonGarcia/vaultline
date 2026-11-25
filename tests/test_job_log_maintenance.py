"""T2 S1-S3 job log lifecycle: runtime policy and Global Settings tests.

Covers the maintenance service (stoppable periodic lane), the SQLite-side
bounded operations (sizes metadata, expire, archive candidates, quota
eviction), the removed-by-policy marker, and the additive Global Settings
validation. The in-memory backend only round-trips the configuration and is
expected to report maintenance as unsupported.
"""

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import brotli
from cryptography.fernet import Fernet

from src.control_plane.application.services import job_log_maintenance
from src.control_plane.application.services.control_plane_service import (
    ControlPlaneService,
    SETTINGS_UNSET,
)
from src.control_plane.domain.models import (
    JobLogPruneConflict,
    JobRecord,
    JobStatus,
    SettingsRecord,
    WorkerRecord,
    effective_job_log_quota_enabled,
    utcnow,
)
from src.control_plane.infrastructure.repositories.in_memory import (
    InMemoryInventoryRepository,
    InMemoryJobRepository,
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
    SQLiteJobRepository,
    SQLiteSettingsRepository,
    job_log_policy_candidates_sql,
)
from src.control_plane.infrastructure.security.secret_codec import SecretCodec

BIG_TEXT = "Backup progress: copied block of data to the repository. " * 40
SECRET_TEXT = "s3:topsecretbucket@eu-central"


def make_service(job_repository, settings_repository=None):
    workers = InMemoryWorkerRepository()
    workers.save(WorkerRecord(name="worker-a", host_name="host", id="worker-a", status="online", last_seen_at=utcnow()))
    return ControlPlaneService(
        worker_repository=workers,
        inventory_repository=InMemoryInventoryRepository(),
        target_repository=InMemoryTargetRepository(),
        job_repository=job_repository,
        storage_profile_repository=InMemoryStorageProfileRepository(),
        secret_repository=InMemorySecretRepository(),
        snapshot_repository=InMemorySnapshotRepository(),
        retention_policy_repository=InMemoryRetentionPolicyRepository(),
        target_stats_repository=InMemoryTargetStatsRepository(),
        secret_codec=SecretCodec(Fernet.generate_key()),
        settings_repository=settings_repository or InMemorySettingsRepository(),
    )


def make_maintenance(service, **kwargs):
    kwargs.setdefault("interval_seconds", 300)
    kwargs.setdefault("batch_size", 25)
    return job_log_maintenance.JobLogMaintenanceService(service, **kwargs)


def stored_log_bytes(database_path, job_id=None):
    connection = sqlite3.connect(database_path)
    try:
        connection.row_factory = sqlite3.Row
        query = (
            "SELECT id, length(CAST(log_lines_json AS BLOB))"
            " + COALESCE(length(CAST(log_timestamps_json AS BLOB)), 0)"
            " + COALESCE(length(job_log_brotli), 0) AS log_bytes FROM jobs"
        )
        parameters = ()
        if job_id is not None:
            query += " WHERE id = ?"
            parameters = (job_id,)
        return {row["id"]: row["log_bytes"] for row in connection.execute(query, parameters).fetchall()}
    finally:
        connection.close()


class SettingsMaintenanceTests(unittest.TestCase):
    def test_maintenance_defaults_validation_and_partial_updates(self):
        service = make_service(InMemoryJobRepository())

        default = service.update_settings()
        self.assertFalse(default.job_log_maintenance_enabled)
        self.assertEqual(default.job_log_archive_after_days, SettingsRecord.DEFAULT_JOB_LOG_ARCHIVE_AFTER_DAYS)
        self.assertEqual(default.job_log_retention_days, SettingsRecord.DEFAULT_JOB_LOG_RETENTION_DAYS)
        self.assertIsNone(default.job_log_quota_gib)

        enabled = service.update_settings(job_log_maintenance_enabled=True)
        self.assertTrue(enabled.job_log_maintenance_enabled)
        self.assertIsNone(enabled.job_log_quota_gib)

        configured = service.update_settings(job_log_archive_after_days=45, job_log_retention_days=120, job_log_quota_gib=8)
        self.assertEqual(configured.job_log_archive_after_days, 45)
        self.assertEqual(configured.job_log_retention_days, 120)
        self.assertEqual(configured.job_log_quota_gib, 8)

        quota_disabled = service.update_settings(job_log_quota_gib=None)
        self.assertIsNone(quota_disabled.job_log_quota_gib)
        self.assertEqual(quota_disabled.job_log_archive_after_days, 45)
        self.assertEqual(quota_disabled.job_log_retention_days, 120)

        omitted = service.update_settings(job_log_quota_gib=SETTINGS_UNSET)
        self.assertIsNone(omitted.job_log_quota_gib)
        self.assertEqual(omitted.job_log_maintenance_enabled, True)

        for invalid_days in (0, -1, True, 2.5, "30"):
            with self.subTest(field="job_log_archive_after_days", value=invalid_days):
                with self.assertRaises(ValueError):
                    service.update_settings(job_log_archive_after_days=invalid_days)
            with self.subTest(field="job_log_retention_days", value=invalid_days):
                with self.assertRaises(ValueError):
                    service.update_settings(job_log_retention_days=invalid_days)

        for invalid_quota in (0, -1, True, 1.5, "8"):
            with self.subTest(field="job_log_quota_gib", value=invalid_quota):
                with self.assertRaises(ValueError):
                    service.update_settings(job_log_quota_gib=invalid_quota)

        # archive < retention is enforced against the merged configuration.
        with self.assertRaisesRegex(ValueError, "less than"):
            service.update_settings(job_log_archive_after_days=120)
        with self.assertRaisesRegex(ValueError, "less than"):
            service.update_settings(job_log_retention_days=44)

    def test_sqlite_settings_roundtrip_preserves_maintenance_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository = SQLiteSettingsRepository(database)
            saved = repository.save(
                SettingsRecord(
                    job_log_maintenance_enabled=True,
                    job_log_archive_after_days=20,
                    job_log_retention_days=70,
                    job_log_quota_gib=12,
                )
            )
            self.assertTrue(saved.job_log_maintenance_enabled)
            self.assertEqual(
                (saved.job_log_archive_after_days, saved.job_log_retention_days, saved.job_log_quota_gib),
                (20, 70, 12),
            )
            reopened = SQLiteSettingsRepository(database).get()
            self.assertEqual(
                (
                    reopened.job_log_maintenance_enabled,
                    reopened.job_log_archive_after_days,
                    reopened.job_log_retention_days,
                    reopened.job_log_quota_gib,
                ),
                (True, 20, 70, 12),
            )

            repository.save(SettingsRecord(job_log_maintenance_enabled=True, job_log_quota_gib=None))
            self.assertTrue(SQLiteSettingsRepository(database).get().job_log_maintenance_enabled)
            self.assertIsNone(SQLiteSettingsRepository(database).get().job_log_quota_gib)
            # A fresh default row round-trips the disabled default.
            repository.save(SettingsRecord())
            fresh = SQLiteSettingsRepository(database).get()
            self.assertFalse(fresh.job_log_maintenance_enabled)
            self.assertIsNone(fresh.job_log_quota_gib)


class SizeMetadataTests(unittest.TestCase):
    def test_triggers_track_stored_bytes_for_save_progress_and_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database)
            service = make_service(repository)
            job = service.dispatch_job("worker-a", "backup.run")
            claimed = service.fetch_jobs_for_worker("worker-a")[0]
            service.update_job_progress(
                "worker-a", job.id, 1, {"phase": "backup"}, [BIG_TEXT, "second line"],
                lease_token=claimed.lease_token,
                log_timestamps=["2024-05-05T10:00:00Z", None],
            )
            service.update_job_status(
                "worker-a", job.id, JobStatus.SUCCEEDED, result_summary={"state": "done"},
                log_lines=["terminal line"], lease_token=claimed.lease_token,
                log_timestamps=["2024-05-05T10:00:01Z"],
            )
            expected = stored_log_bytes(database)
            self.assertEqual(repository.job_log_usage()["measured_bytes"], sum(expected.values()))
            usage_after_terminal = expected
            # INSERT OR REPLACE (save) keeps the metadata exact for raw rewrites.
            current = repository.get(job.id)
            current.log_lines = [BIG_TEXT]
            repository.save(current)
            expected = stored_log_bytes(database)
            self.assertEqual(repository.job_log_usage()["measured_bytes"], sum(expected.values()))
            raw_before_archive = expected[job.id]
            cutoff = utcnow() - timedelta(days=31)
            finished = repository.get(job.id)
            finished.finished_at = cutoff
            repository.save(finished)
            self.assertGreater(repository.archive_job_log(job.id, cutoff), 0)
            expected = stored_log_bytes(database)
            self.assertEqual(repository.job_log_usage()["measured_bytes"], sum(expected.values()))
            # The archive stores far fewer bytes than the raw payload did.
            self.assertLess(expected[job.id], raw_before_archive)

    def test_legacy_backfill_is_bounded_lazy_and_lazily_marks_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database)
            for index in range(5):
                repository.save(JobRecord(
                    worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
                    log_lines=[f"{index}-" + BIG_TEXT], finished_at=utcnow() - timedelta(days=60),
                ))
            # Simulate legacy rows recorded before the metadata table existed.
            connection = sqlite3.connect(database)
            try:
                connection.execute("DELETE FROM job_log_sizes")
                connection.execute("DROP TRIGGER trg_job_log_sizes_insert")
                connection.execute("DROP TRIGGER trg_job_log_sizes_update")
                connection.commit()
            finally:
                connection.close()

            usage = repository.job_log_usage()
            self.assertEqual(usage["measured_bytes"], 0)
            self.assertFalse(usage["complete"])

            first = repository.backfill_job_log_sizes(batch_size=2)
            self.assertEqual(first["backfilled"], 2)
            self.assertFalse(first["complete"])
            self.assertFalse(repository.job_log_usage()["complete"])
            repository.backfill_job_log_sizes(batch_size=2)
            third = repository.backfill_job_log_sizes(batch_size=2)
            self.assertEqual(third["backfilled"], 1)
            self.assertTrue(third["complete"])
            expected = stored_log_bytes(database)
            self.assertEqual(
                repository.job_log_usage()["measured_bytes"],
                sum(expected.values()),
            )
            self.assertTrue(repository.job_log_usage()["complete"])
            # Idempotent: a further pass finds nothing to do.
            done = repository.backfill_job_log_sizes(batch_size=2)
            self.assertEqual(done["backfilled"], 0)
            self.assertTrue(done["complete"])


class CompactSelectionPathTests(unittest.TestCase):
    """B1: policy selection must be a covering path over compact metadata.

    Wide jobs rows (400 rows x 2 MiB logs on a 24 GiB database) made the
    batch-0 selection queries cost ~0.47 s while the same shape against tiny
    log rows stayed under 0.01 s. Selection and ordering must therefore read
    ONLY the compact metadata (trigger-maintained flags + policy index); the
    wide rows are touched again only per selected candidate (bounded by the
    batch limit) for the recheck and the compare-and-swap.
    """

    def _plan_details(self, database, sql, parameters):
        connection = sqlite3.connect(database)
        try:
            return [
                row[-1]
                for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters).fetchall()
            ]
        finally:
            connection.close()

    def test_policy_selection_plans_never_touch_wide_jobs_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            SQLiteJobRepository(database)
            shapes = (
                (job_log_policy_candidates_sql(with_cutoff=True, require_unarchived=False),
                 {"cutoff": "2026-01-01", "limit": 25}),
                (job_log_policy_candidates_sql(with_cutoff=False, require_unarchived=False),
                 {"limit": 25}),
                (job_log_policy_candidates_sql(with_cutoff=True, require_unarchived=True),
                 {"cutoff": "2026-01-01", "limit": 25}),
                (job_log_policy_candidates_sql(with_cutoff=True, require_unarchived=False, exclude_count=1),
                 ("2026-01-01", "excluded-id", 25)),
            )
            for sql, parameters in shapes:
                with self.subTest(sql=sql[:60]):
                    details = self._plan_details(database, sql, parameters)
                    self.assertTrue(details)
                    joined = " | ".join(details)
                    # Every step works on the compact metadata only: no step
                    # scans or searches the wide jobs table.
                    for detail in details:
                        self.assertIn("job_log_sizes", detail)
                        self.assertNotIn("TABLE jobs", detail)
                        self.assertNotIn("SCAN jobs", detail)
                    self.assertIn("idx_job_log_sizes_policy", joined)

    def test_compact_flags_columns_index_and_trigger_tracking(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            SQLiteJobRepository(database)
            connection = sqlite3.connect(database)
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(job_log_sizes)")}
                for column in ("finished_at", "terminal", "payload_present", "archived", "removed"):
                    self.assertIn(column, columns)
                indexes = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )}
                self.assertIn("idx_job_log_sizes_policy", indexes)
            finally:
                connection.close()

            repository = SQLiteJobRepository(database)
            service = make_service(repository)
            job = service.dispatch_job("worker-a", "backup.run")
            claimed = service.fetch_jobs_for_worker("worker-a")[0]
            metadata = repository._policy_metadata_for(job.id)
            self.assertEqual(metadata["terminal"], 0)
            self.assertEqual(metadata["payload_present"], 0)  # no logs yet
            self.assertEqual(metadata["archived"], 0)
            self.assertEqual(metadata["removed"], 0)

            service.update_job_progress(
                "worker-a", job.id, 1, {"phase": "b"}, [BIG_TEXT],
                lease_token=claimed.lease_token,
            )
            metadata = repository._policy_metadata_for(job.id)
            self.assertEqual(metadata["payload_present"], 1)

            service.update_job_status(
                "worker-a", job.id, JobStatus.SUCCEEDED,
                result_summary={"state": "done"}, log_lines=["terminal"],
                lease_token=claimed.lease_token,
            )
            metadata = repository._policy_metadata_for(job.id)
            self.assertEqual(metadata["terminal"], 1)

            cutoff = utcnow() - timedelta(days=31)
            finished = repository.get(job.id)
            finished.finished_at = cutoff
            repository.save(finished)
            self.assertGreater(repository.archive_job_log(job.id, cutoff), 0)
            metadata = repository._policy_metadata_for(job.id)
            self.assertEqual(metadata["archived"], 1)
            self.assertEqual(metadata["payload_present"], 1)

            repository.expire_job_logs(cutoff, limit=10)
            metadata = repository._policy_metadata_for(job.id)
            self.assertEqual(metadata["removed"], 1)
            self.assertEqual(metadata["payload_present"], 0)

    def test_batch0_rows_without_flags_reopen_without_marker_and_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database)
            repository.save(JobRecord(
                worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
                log_lines=[BIG_TEXT], finished_at=utcnow() - timedelta(days=60),
            ))
            repository.backfill_job_log_sizes(batch_size=5)
            self.assertTrue(repository.job_log_usage()["complete"])
            # Simulate a batch-0 metadata row (sizes kept, flags never set).
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "UPDATE job_log_sizes SET terminal = NULL, payload_present = NULL, "
                    "archived = NULL, removed = NULL, finished_at = NULL"
                )
                connection.commit()
            finally:
                connection.close()

            reopened = SQLiteJobRepository(database)
            self.assertFalse(reopened.job_log_usage()["complete"])
            done = reopened.backfill_job_log_sizes(batch_size=5)
            self.assertEqual(done["backfilled"], 1)
            self.assertTrue(done["complete"])

            row = reopened._policy_metadata_for(
                reopened.list()[0].id
            )
            self.assertEqual(row["terminal"], 1)
            self.assertEqual(row["payload_present"], 1)
            self.assertEqual(row["removed"], 0)


class QuotaBudgetTests(unittest.TestCase):
    """B2: eviction is a strict-`>` byte-budget loop, never a batch blast.

    The eviction transaction measures the compact total, requires a byte
    budget, deletes one candidate at a time and stops the moment the measured
    total is <= budget. A concurrent PATCH's stored quota (same database)
    overrides the caller's stale threshold inside the same transaction, and
    usage exactly at quota is never treated as excess.
    """

    # log rows are '["xxxxxxxxxx"]' (14 bytes) + timestamps (24 bytes) and
    # '["yyyyyyyyyyyyyyyyyyyy"]' (24 bytes + 0 without timestamps).
    OLDER_BYTES = 38
    NEWER_BYTES = 24
    # A removed row keeps a 2-byte '[]' sentinel residue in the metadata.
    SENTINEL_BYTES = 2

    @staticmethod
    def _seed(database):
        repository = SQLiteJobRepository(database)
        cutoff = utcnow() - timedelta(days=40)
        older = repository.save(JobRecord(
            worker_id="worker-a", command="backup.run", id="job-older",
            status=JobStatus.SUCCEEDED, log_lines=["x" * 10],
            log_timestamps=["2024-05-05T10:00:00Z"],
            finished_at=cutoff - timedelta(days=2),
        ))
        newer = repository.save(JobRecord(
            worker_id="worker-a", command="backup.run", id="job-newer",
            status=JobStatus.SUCCEEDED, log_lines=["y" * 20],
            finished_at=cutoff - timedelta(days=1),
        ))
        measured = repository.job_log_usage()["measured_bytes"]
        self_check = measured - (QuotaBudgetTests.OLDER_BYTES + QuotaBudgetTests.NEWER_BYTES)
        return repository, measured, self_check

    def test_exact_quota_never_deletes_and_penny_excess_removes_one_row(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, measured, delta = self._seed(f"{directory}/cp.db")
            self.assertEqual(delta, 0)  # fixture measures exactly the two rows

            # Exactly at quota: strict `>` means no deletion at all.
            at_quota = repository.evict_job_logs_for_quota(
                limit=25, free_to_bytes=measured
            )
            self.assertEqual(at_quota["evicted"], 0)
            self.assertEqual(at_quota["bytes_freed"], 0)
            self.assertEqual(at_quota["jobs"], [])
            self.assertEqual(repository.job_log_usage()["measured_bytes"], measured)

            # Excess smaller than one row with a batch of 25 candidates:
            # exactly the oldest log is deleted and the loop stops as soon as
            # the measured total is <= budget again.
            over = repository.evict_job_logs_for_quota(
                limit=25, free_to_bytes=measured - 1
            )
            self.assertEqual(over["evicted"], 1)
            self.assertEqual(over["jobs"], ["job-older"])
            self.assertEqual(over["bytes_freed"], self.OLDER_BYTES)
            self.assertEqual(
                over["measured_bytes"],
                measured - self.OLDER_BYTES + self.SENTINEL_BYTES,
            )
            self.assertLessEqual(over["measured_bytes"], measured - 1)

            # A second identical call re-measures inside its own transaction
            # and finds nothing more to do.
            again = repository.evict_job_logs_for_quota(
                limit=25, free_to_bytes=measured - 1
            )
            self.assertEqual(again["evicted"], 0)
            self.assertEqual(
                again["measured_bytes"],
                measured - self.OLDER_BYTES + self.SENTINEL_BYTES,
            )

    def test_budget_is_required_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, _, delta = self._seed(f"{directory}/cp.db")
            self.assertEqual(delta, 0)
            with self.assertRaises(TypeError):
                repository.evict_job_logs_for_quota(limit=25)
            with self.assertRaisesRegex(ValueError, "budget"):
                repository.evict_job_logs_for_quota(limit=25, free_to_bytes=-1)

    def test_service_phase3_strict_gt_and_budget_stop_mid_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteJobRepository(f"{directory}/cp.db")
            service = make_service(repository)
            cutoff_days = 40
            for line, age, timestamps in (
                (["x" * 10], cutoff_days, ["2024-05-05T10:00:00Z"]),
                (["y" * 20], cutoff_days - 1, None),
            ):
                repository.save(JobRecord(
                    worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
                    log_lines=line, log_timestamps=timestamps,
                    finished_at=utcnow() - timedelta(days=age),
                ))
            measured = repository.job_log_usage()["measured_bytes"]
            lane = make_maintenance(service, now_fn=lambda: utcnow())
            service.update_settings(
                job_log_maintenance_enabled=True,
                # Archive age above the jobs' age keeps the archive phase out
                # of this test's cool-downs; the eviction budget is exercised.
                job_log_archive_after_days=50,
                job_log_retention_days=90,
            )
            # Quota sits 38 bytes below the total: one row frees enough, so
            # the bounded batch stops after a single deletion.
            service.update_settings(job_log_quota_gib=3)
            with mock.patch(job_log_maintenance.__name__ + ".BYTES_PER_GIB", 10):
                with mock.patch(
                    "src.control_plane.infrastructure.repositories.sqlite.JOB_LOG_QUOTA_GIB_TO_BYTES", 10
                ):
                    result = lane.run_once()
            self.assertTrue(result["quota_breached"])
            self.assertEqual(result["actions"]["evicted"], 1)
            self.assertEqual(result["actions"]["evicted_bytes_freed"], self.OLDER_BYTES)
            after = repository.job_log_usage()["measured_bytes"]
            self.assertEqual(after, measured - self.OLDER_BYTES + 2)
            self.assertLessEqual(after, 30)

    def test_service_phase3_exact_quota_deletes_nothing_and_is_not_a_breach(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteJobRepository(f"{directory}/cp.db")
            service = make_service(repository)
            repository.save(JobRecord(
                worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
                log_lines=["x" * 10], log_timestamps=["2024-05-05T10:00:00Z"],
                finished_at=utcnow() - timedelta(days=40),
            ))
            measured = repository.job_log_usage()["measured_bytes"]
            lane = make_maintenance(service, now_fn=lambda: utcnow())
            service.update_settings(
                job_log_maintenance_enabled=True,
                job_log_archive_after_days=5,
                job_log_retention_days=90,
                # Quota exactly equal to the measured total is not excess.
                job_log_quota_gib=measured,
            )
            with mock.patch(job_log_maintenance.__name__ + ".BYTES_PER_GIB", 1):
                with mock.patch(
                    "src.control_plane.infrastructure.repositories.sqlite.JOB_LOG_QUOTA_GIB_TO_BYTES", 1
                ):
                    with self.assertNoLogs("src.control_plane.application.services.job_log_maintenance", level="WARNING"):
                        result = lane.run_once()
            self.assertFalse(result["quota_breached"])
            self.assertNotIn("evicted", result["actions"])
            self.assertEqual(repository.job_log_usage()["measured_bytes"], measured)

    def test_clearing_or_disabling_the_stored_quota_cancels_a_stale_eviction(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/cp.db"
            repository, measured, _ = self._seed(database)
            settings = SQLiteSettingsRepository(database)
            for enabled, quota in ((False, 1), (True, None)):
                with self.subTest(enabled=enabled, quota=quota):
                    settings.save(SettingsRecord(
                        job_log_maintenance_enabled=enabled, job_log_quota_gib=quota,
                    ))
                    result = repository.evict_job_logs_for_quota(free_to_bytes=0)
                    self.assertEqual(result["evicted"], 0)
                    self.assertEqual(result["measured_bytes"], measured)
                    self.assertEqual(repository.get("job-older").log_lines, ["x" * 10])

    def test_two_independent_instances_read_current_quota_measured_in_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/cp.db"
            # Seed with a first instance; open a second independent instance
            # sharing the same database and the same stored quota row.
            repository, measured, delta = self._seed(database)
            self.assertEqual(delta, 0)
            second = SQLiteJobRepository(database)
            # A concurrent PATCH stores a smaller quota in the shared row.
            SQLiteSettingsRepository(database).save(SettingsRecord(
                job_log_maintenance_enabled=True,
                job_log_quota_gib=3,
            ))

            with mock.patch(
                "src.control_plane.infrastructure.repositories.sqlite.JOB_LOG_QUOTA_GIB_TO_BYTES", 10
            ):
                # The lane's stale threshold believes a far larger quota; the
                # stored 3 GiB (30 bytes at test scale) wins inside the
                # transaction: one row is freed to reach it, never more.
                first = repository.evict_job_logs_for_quota(
                    limit=25, free_to_bytes=10 ** 9
                )
                self.assertEqual(first["budget_bytes"], 30)
                self.assertEqual(first["evicted"], 1)
                self.assertEqual(
                    first["measured_bytes"],
                    measured - self.OLDER_BYTES + self.SENTINEL_BYTES,
                )
                self.assertLessEqual(first["measured_bytes"], 30)

                stale = second.evict_job_logs_for_quota(
                    limit=25, free_to_bytes=10 ** 9
                )
                self.assertEqual(stale["budget_bytes"], 30)
                self.assertEqual(stale["evicted"], 0)
                self.assertEqual(stale["measured_bytes"], first["measured_bytes"])


class RemovalPolicyTests(unittest.TestCase):
    def _make_jobs(self, database):
        repository = SQLiteJobRepository(database)
        cutoff = utcnow() - timedelta(days=40)
        finished_old = cutoff - timedelta(days=1)
        finished_recent = utcnow() - timedelta(days=1)
        made = {}
        made["expired"] = repository.save(JobRecord(
            id="b-expired", worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
            log_lines=[BIG_TEXT], log_timestamps=["2024-05-05T10:00:00Z"],
            finished_at=finished_old, result_summary={"status_code": 0},
        ))
        made["recent"] = repository.save(JobRecord(
            worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
            log_lines=[BIG_TEXT], finished_at=finished_recent,
        ))
        made["same_time_b"] = repository.save(JobRecord(
            id="c-same-time", worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
            log_lines=[BIG_TEXT], finished_at=finished_old,
        ))
        made["diagnostic"] = repository.save(JobRecord(
            id="a-diagnostic", worker_id="worker-a", command="backup.run", status=JobStatus.FAILED,
            log_lines=[BIG_TEXT], finished_at=finished_old,
            result_summary={"diagnostic_code": "worker_lease_expired"},
        ))
        made["unconfirmed"] = repository.save(JobRecord(
            worker_id="worker-a", command="backup.run", status=JobStatus.UNCONFIRMED,
            log_lines=[BIG_TEXT], finished_at=finished_old,
        ))
        made["empty"] = repository.save(JobRecord(
            worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
            log_lines=[], finished_at=finished_old,
        ))
        made["legacy_cancelled"] = repository.save(
            JobRecord(
                worker_id="worker-a", command="backup.run", status="cancelled",
                log_lines=[BIG_TEXT], finished_at=cutoff,
            )
        )
        connection = sqlite3.connect(database)
        try:
            connection.execute("UPDATE jobs SET status = 'cancelled' WHERE id = ?", (made["legacy_cancelled"].id,))
            connection.commit()
        finally:
            connection.close()
        return repository, made, cutoff

    def test_expire_preserves_jobs_and_only_touches_eligible_logs_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository, made, cutoff = self._make_jobs(database)

            result = repository.expire_job_logs(cutoff, limit=10)

            # Terminal finished jobs past the cutoff go, legacy 'cancelled'
            # spelling included; active/synthetic-late-report-capable and
            # empty rows never do.
            self.assertEqual(result["expired"], 3)
            self.assertGreater(result["bytes_freed"], 0)
            self.assertEqual(
                set(result["jobs"]),
                {made["expired"].id, made["same_time_b"].id, made["legacy_cancelled"].id},
            )
            for job_id in ("expired", "same_time_b", "legacy_cancelled"):
                stored = repository.get(made[job_id].id)
                self.assertTrue(getattr(stored, "job_log_removed", False))
                self.assertEqual(stored.log_lines, [])
                self.assertIsNone(stored.log_timestamps)
            for job_id in ("recent", "diagnostic", "unconfirmed", "empty"):
                stored = repository.get(made[job_id].id)
                self.assertFalse(getattr(stored, "job_log_removed", False))
                if job_id != "empty":
                    self.assertEqual(stored.log_lines, [BIG_TEXT])
                else:
                    self.assertEqual(stored.log_lines, [])
            # The expired jobs' rows keep status and result summary intact.
            self.assertEqual(repository.get(made["expired"].id).result_summary, {"status_code": 0})
            self.assertEqual(repository.get(made["same_time_b"].id).status, JobStatus.SUCCEEDED)
            # Metadata is updated with the sentinel accounting, not dropped.
            self.assertIn(made["expired"].id, stored_log_bytes(database))

            again = repository.expire_job_logs(cutoff, limit=10)
            self.assertEqual(again["expired"], 0)
            self.assertEqual(again["bytes_freed"], 0)

    def test_expire_limit_and_deterministic_tie_breaker(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository, made, cutoff = self._make_jobs(database)
            result = repository.expire_job_logs(cutoff, limit=1)
            # The oldest finished jobs share one finish time; the id is the
            # deterministic tie-breaker over the compact selection.
            self.assertEqual(result["jobs"], [])
            self.assertEqual(result["skipped"], [made["diagnostic"].id])
            next_batch = repository.expire_job_logs(cutoff, limit=1, exclude_ids=set(result["skipped"]))
            self.assertEqual(next_batch["jobs"], [made["expired"].id])

    def test_removed_by_policy_marker_distinguishes_removal_from_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository, made, cutoff = self._make_jobs(database)
            service = make_service(repository)
            repository.expire_job_logs(cutoff, limit=10)

            removed_view = service.get_job_view(made["expired"].id)
            self.assertEqual(removed_view["log_lines"], [])
            self.assertTrue(removed_view["log_removed_by_policy"])
            # No fake log lines are injected.
            self.assertNotIn("removed", removed_view["log_lines"])

            empty_view = service.get_job_view(made["empty"].id)
            self.assertEqual(empty_view["log_lines"], [])
            self.assertFalse(empty_view.get("log_removed_by_policy", False))

    def test_evict_oldest_finished_eligible_logs_regardless_of_age(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository, made, cutoff = self._make_jobs(database)

            guarded = repository.evict_job_logs_for_quota(limit=1, free_to_bytes=0)
            self.assertEqual(guarded["evicted"], 0)
            self.assertEqual(guarded["skipped"], [made["diagnostic"].id])
            self.assertEqual(guarded["bytes_freed"], 0)

            result = repository.evict_job_logs_for_quota(
                limit=1, exclude_ids=set(guarded["skipped"]), free_to_bytes=0
            )
            self.assertEqual(result["evicted"], 1)
            self.assertEqual(result["jobs"], [made["expired"].id])
            self.assertGreater(result["bytes_freed"], 0)
            # The recent finished job is evictable too: the quota policy is
            # not bound by the archive age cutoff (S2: only the last 3 months
            # of logs are kept, but a quota evicts even newer ones).
            second = repository.evict_job_logs_for_quota(limit=10, free_to_bytes=0)
            self.assertEqual(
                set(second["jobs"]),
                {made["expired"].id, made["same_time_b"].id, made["legacy_cancelled"].id, made["recent"].id}
                - {result["jobs"][0]},
            )
            for skipped in ("diagnostic", "unconfirmed"):
                stored = repository.get(made[skipped].id)
                self.assertEqual(stored.log_lines, [BIG_TEXT])
                self.assertFalse(getattr(stored, "job_log_removed", False))
            exhausted = repository.evict_job_logs_for_quota(limit=10, free_to_bytes=0)
            self.assertEqual(exhausted["evicted"], 0)
            self.assertEqual(exhausted["jobs"], [])

    def test_archive_candidates_are_bounded_deterministic_and_covering(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository, made, cutoff = self._make_jobs(database)
            # B1: compact selection cannot see result summaries, so the
            # late-report-capable diagnostic row appears as a candidate; the
            # full T1 eligibility (including the diagnostic guard) is rechecked
            # by the compare-and-swap, which refuses it without any loss.
            candidate_ids = {
                made["expired"].id, made["same_time_b"].id,
                made["legacy_cancelled"].id, made["diagnostic"].id,
            }
            candidates = repository.archive_job_log_candidates(
                cutoff, limit=10
            )
            self.assertEqual(set(candidates), candidate_ids)
            limited = repository.archive_job_log_candidates(cutoff, limit=1)
            self.assertEqual(limited, [min(made["expired"].id, made["same_time_b"].id, made["diagnostic"].id)])
            excluded = repository.archive_job_log_candidates(
                cutoff, limit=10, exclude_ids=candidate_ids - {made["diagnostic"].id}
            )
            self.assertEqual(excluded, [made["diagnostic"].id])
            first = repository.archive_job_log(made["expired"].id, cutoff)
            self.assertGreater(first, 0)
            # The guarded row is rejected by the CAS, not by the selection.
            self.assertEqual(repository.archive_job_log(made["diagnostic"].id, cutoff), 0)
            after = repository.archive_job_log_candidates(cutoff, limit=10)
            self.assertEqual(set(after), candidate_ids - {made["expired"].id})

    def test_removed_by_policy_marker_survives_and_resets_on_raw_rewrites(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository, made, cutoff = self._make_jobs(database)
            removed_id = made["expired"].id
            repository.expire_job_logs(cutoff, limit=10)
            self.assertTrue(repository.get(removed_id).job_log_removed)

            # A full-record save of a fresh record is the authoritative rewrite:
            # the raw log returns and the marker resets, keeping the size
            # metadata exact through every path.
            replacement = JobRecord(
                id=removed_id, worker_id="worker-a", command="backup.run",
                status=JobStatus.SUCCEEDED, log_lines=[BIG_TEXT],
                finished_at=utcnow() - timedelta(days=40),
            )
            repository.save(replacement)
            stored = repository.get(removed_id)
            self.assertFalse(getattr(stored, "job_log_removed", False))
            self.assertEqual(stored.log_lines, [BIG_TEXT])
            self.assertEqual(
                repository.job_log_usage()["measured_bytes"],
                sum(stored_log_bytes(database).values()),
            )

    def test_policy_events_are_recorded_bounded_and_ordered(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository, made, cutoff = self._make_jobs(database)
            self.assertEqual(repository.job_log_policy_events(), [])
            for index in range(25):
                repository.record_job_log_policy_events([
                    {"at": f"2024-05-05T10:00:{index:02d}Z", "kind": "logs_expired", "job_count": 1},
                ])
            events = repository.job_log_policy_events()
            self.assertEqual(len(events), 20)
            self.assertEqual(events[-1]["at"], "2024-05-05T10:00:24Z")


class MaintenanceServiceTests(unittest.TestCase):
    def test_disabled_retention_config_still_archives_but_never_deletes(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteJobRepository(f"{directory}/control-plane.db")
            service = make_service(repository)
            repository.save(JobRecord(
                worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
                log_lines=[BIG_TEXT], finished_at=utcnow() - timedelta(days=120),
            ))
            lane = make_maintenance(service)
            result = lane.run_once()

            self.assertTrue(result["supported"])
            self.assertFalse(result["enabled"])
            # S5: archival is automatic, but nothing is deleted while the
            # retention opt-in and the quota are both disabled.
            self.assertGreaterEqual(result["actions"]["archived"], 1)
            self.assertNotIn("expired", result["actions"])
            self.assertNotIn("evicted", result["actions"])
            stored = repository.list()[0]
            self.assertFalse(getattr(stored, "job_log_removed", False))
            self.assertTrue(repository.job_log_usage()["complete"])
            self.assertTrue(any(event["kind"] == "logs_archived" for event in repository.job_log_policy_events()))
            self.assertEqual(
                [event["kind"] for event in repository.job_log_policy_events() if event["kind"] not in ("logs_archived",)],
                [],
            )

    def test_tick_runs_expire_then_archive_then_quota_eviction_with_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteJobRepository(f"{directory}/control-plane.db")
            service = make_service(repository)
            secret_job = repository.save(JobRecord(
                worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
                log_lines=[SECRET_TEXT + BIG_TEXT], log_timestamps=["2024-05-05T10:00:00Z"],
                finished_at=utcnow() - timedelta(days=40),
                result_summary={"status_code": 0},
            ))
            service.update_settings(
                job_log_maintenance_enabled=True,
                job_log_archive_after_days=30,
                job_log_retention_days=90,
                job_log_quota_gib=1,
            )
            lane = make_maintenance(service, now_fn=lambda: utcnow())
            recorded = []
            original_expire = repository.expire_job_logs
            original_candidates = repository.archive_job_log_candidates

            def spy_expire(cutoff, limit, exclude_ids=None):
                recorded.append("expire")
                return original_expire(cutoff, limit, exclude_ids=exclude_ids)

            def spy_candidates(cutoff, limit, exclude_ids=None):
                recorded.append("archive_candidates")
                return original_candidates(cutoff, limit, exclude_ids)

            with mock.patch(job_log_maintenance.__name__ + ".BYTES_PER_GIB", 1):
                with mock.patch.object(repository, "expire_job_logs", spy_expire), mock.patch.object(
                    repository, "archive_job_log_candidates", spy_candidates
                ):
                    with self.assertLogs("src.control_plane.application.services.job_log_maintenance", level="WARNING") as captured:
                        result = lane.run_once()

            self.assertEqual(recorded, ["expire", "archive_candidates"])
            self.assertTrue(result["quota_breached"])
            self.assertGreaterEqual(result["actions"]["archived"], 1)
            self.assertGreaterEqual(result["actions"]["archived_bytes_saved"], 1)
            self.assertGreaterEqual(result["actions"]["evicted"], 1)
            stored = repository.get(secret_job.id)
            self.assertTrue(getattr(stored, "job_log_removed", False))
            # Job, summary and status survive the policy; only the log payload is gone.
            self.assertEqual(stored.status, JobStatus.SUCCEEDED)
            self.assertEqual(stored.result_summary, {"status_code": 0})
            warnings_text = "\n".join(captured.output)
            self.assertIn("quota", warnings_text)
            for secret_fragment in (SECRET_TEXT, BIG_TEXT):
                self.assertNotIn(secret_fragment, warnings_text)

            events = repository.job_log_policy_events()
            kinds = {event["kind"] for event in events}
            self.assertIn("quota_breach", kinds)
            self.assertIn("logs_evicted", kinds)
            breach = [event for event in events if event["kind"] == "quota_breach"][-1]
            self.assertIn("measured_bytes", breach)

            # The breach stays visible even when automatic eviction drops the
            # current usage back below a larger (new) quota.
            service.update_settings(job_log_quota_gib=1024)
            status = service.job_log_maintenance_status(maintenance=lane)
            self.assertTrue(status["supported"])
            self.assertTrue(status["enabled"])
            self.assertTrue(status["quota_breached"])
            self.assertGreater(status["usage"]["measured_bytes"], 0)
            self.assertLess(status["usage"]["measured_bytes"], 1024)
            self.assertTrue(any(event["kind"] == "quota_breach" for event in status["events"]))
            self.assertEqual(status["interval_seconds"], 300)

    def test_incompressible_log_is_cooldown_and_other_candidates_still_run(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteJobRepository(f"{directory}/control-plane.db")
            service = make_service(repository)
            stubborn = repository.save(JobRecord(
                worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
                log_lines=["x" * 16], log_timestamps=["2024-05-05T10:00:00Z"],
                finished_at=utcnow() - timedelta(days=10),
            ))
            normal = repository.save(JobRecord(
                worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
                log_lines=[BIG_TEXT], finished_at=utcnow() - timedelta(days=9),
            ))
            service.update_settings(
                job_log_maintenance_enabled=True,
                job_log_archive_after_days=5,
                job_log_retention_days=90,
            )
            lane = make_maintenance(service)
            original_compress = brotli.compress

            def compress_stub(data, **kwargs):
                if b"xxxx" in bytes(data):
                    return bytes(data)  # Incompressible: no saving at all.
                return original_compress(data, **kwargs)

            with mock.patch.object(brotli, "compress", side_effect=compress_stub):
                first = lane.run_once()
            self.assertEqual(first["actions"]["archived"], 1)
            self.assertEqual(first["actions"]["archive_skipped"], 1)

            def stored_raw(job_id):
                connection = sqlite3.connect(f"{directory}/control-plane.db")
                try:
                    return connection.execute(
                        "SELECT log_lines_json, job_log_brotli FROM jobs WHERE id = ?",
                        (job_id,),
                    ).fetchone()
                finally:
                    connection.close()

            normal_raw = stored_raw(normal.id)
            self.assertEqual(normal_raw[0], "[]")
            self.assertIsNotNone(normal_raw[1])
            stubborn_raw = stored_raw(stubborn.id)
            self.assertNotEqual(stubborn_raw[0], "[]")
            self.assertIsNone(stubborn_raw[1])

            # The next tick neither retries the stubborn log nor removes it.
            with mock.patch.object(brotli, "compress", side_effect=compress_stub):
                second = lane.run_once()
            self.assertEqual(second["actions"]["archived"], 0)
            self.assertEqual(second["actions"]["archive_skipped"], 0)
            stubborn_raw_two = stored_raw(stubborn.id)
            self.assertEqual(stubborn_raw_two, stubborn_raw)

    def test_in_memory_backend_reports_maintenance_unsupported(self):
        service = make_service(InMemoryJobRepository())
        lane = make_maintenance(service)
        result = lane.run_once()
        self.assertFalse(result["supported"])
        self.assertIn("nonpersistent", result["message"].lower())
        status = service.job_log_maintenance_status(maintenance=lane)
        self.assertFalse(status["supported"])
        self.assertIsNone(status["usage"])
        self.assertEqual(status["events"], [])

    def test_lane_start_and_stop_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteJobRepository(f"{directory}/control-plane.db")
            service = make_service(repository)
            lane = make_maintenance(service)
            lane.start()
            self.assertTrue(lane._thread.is_alive())
            lane.stop()
            self.assertFalse(lane._thread.is_alive())
            lane.stop()  # Idempotent.

    def test_concurrent_manual_and_periodic_ticks_never_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteJobRepository(f"{directory}/control-plane.db")
            service = make_service(repository)
            lane = make_maintenance(service)
            service.update_settings(job_log_maintenance_enabled=True)
            entered = threading.Event()
            release = threading.Event()
            spans = []
            original_execute = lane._execute
            first_done = threading.Event()

            def slow_execute(now):
                span = {"start": time.monotonic()}
                spans.append(span)
                entered.set()
                release.wait(2)
                span["end"] = time.monotonic()
                return original_execute(now)

            with mock.patch.object(lane, "_execute", side_effect=slow_execute):
                worker = threading.Thread(target=lane.run_once)
                worker.start()
                self.assertTrue(entered.wait(2))
                parallel = threading.Thread(target=lambda: lane.run_once())
                parallel.start()
                parallel.join(1)
                # The second tick is still serialized behind the first one.
                parallel_done = not parallel.is_alive()
                release.set()
                worker.join(5)
                parallel.join(5)
            self.assertEqual(len(spans), 2)
            spans_by_start = sorted(spans, key=lambda span: span["start"])
            self.assertGreaterEqual(spans_by_start[1]["start"], spans_by_start[0]["end"])
            self.assertFalse(parallel_done)
            self.assertEqual(lane.status()["last_result"]["enabled"], True)


class JobLogPruneRequestTests(unittest.TestCase):
    """S4: admin cutoff prune requests validate strictly, are idempotent for
    the same cutoff and reject conflicting pending prunes while the stored
    log bytes remain untouched."""

    def _make(self, directory):
        database = f"{directory}/cp.db"
        repository = SQLiteJobRepository(database)
        service = make_service(repository)
        for index in range(2):
            repository.save(JobRecord(
                id=f"job-{index}", worker_id="worker-a", command="backup.run",
                status=JobStatus.SUCCEEDED, log_lines=[BIG_TEXT],
                finished_at=utcnow() - timedelta(days=60),
            ))
        return repository, service

    def test_request_requires_acknowledgment_and_strict_past_calendar_date(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, service = self._make(directory)
            for acknowledge in (False, None, "yes", 1):
                with self.subTest(acknowledge=acknowledge):
                    with self.assertRaisesRegex(ValueError, "acknowledg"):
                        service.request_job_log_prune("2026-03-03", acknowledge=acknowledge)
            for bad_cutoff in (True, 20260303, None, "2026-3-3", "03/03/2026",
                               "2026-02-30", "2026-13-01", "2026-03-03 ", ""):
                with self.subTest(cutoff=bad_cutoff):
                    with self.assertRaisesRegex(ValueError, "cutoff_date"):
                        service.request_job_log_prune(bad_cutoff, acknowledge=True)
            future = (utcnow() + timedelta(days=2)).strftime("%Y-%m-%d")
            with self.assertRaisesRegex(ValueError, "future"):
                service.request_job_log_prune(future, acknowledge=True)
            self.assertIsNone(repository.job_log_prune_state())

    def test_accepted_request_persists_pending_state_with_exclusive_next_midnight_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, service = self._make(directory)
            result = service.request_job_log_prune("2026-03-03", acknowledge=True, now=utcnow())
            self.assertTrue(result["created"])
            self.assertEqual(result["cutoff_date"], "2026-03-03")
            self.assertEqual(result["cutoff"], "2026-03-04T00:00:00")
            self.assertEqual(result["status"], "pending")
            self.assertEqual(result["processed"], 0)
            self.assertEqual(result["bytes_freed"], 0)
            state = repository.job_log_prune_state()
            self.assertIsNotNone(state)
            self.assertEqual(state["cutoff_date"], "2026-03-03")
            self.assertEqual(state["status"], "pending")
            # The clear before-or-on-selected-date (UTC) semantics shipped with
            # the state; no historical counts are fabricated.
            self.assertIn("before or on", result["semantics"])
            self.assertNotIn("deleted_jobs", result)

    def test_same_cutoff_is_idempotent_and_conflicting_pending_prune_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, service = self._make(directory)
            first = service.request_job_log_prune("2026-03-03", acknowledge=True)
            self.assertTrue(first["created"])

            second = service.request_job_log_prune("2026-03-03", acknowledge=True)
            self.assertFalse(second["created"])
            self.assertEqual(second["cutoff_date"], "2026-03-03")
            self.assertEqual(second["processed"], 0)

            with self.assertRaises(JobLogPruneConflict):
                service.request_job_log_prune("2026-02-20", acknowledge=True)
            state = repository.job_log_prune_state()
            self.assertEqual(state["cutoff_date"], "2026-03-03")

    def test_invalid_and_conflicting_requests_keep_logs_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, service = self._make(directory)
            before = stored_log_bytes(f"{directory}/cp.db")
            self.assertGreater(sum(before.values()), 0)
            service.request_job_log_prune("2026-03-03", acknowledge=True)
            with self.assertRaises(JobLogPruneConflict):
                service.request_job_log_prune("2026-02-20", acknowledge=True)
            with self.assertRaises(ValueError):
                service.request_job_log_prune("31/12/2025", acknowledge=True)
            with self.assertRaises(ValueError):
                service.request_job_log_prune("2026-03-03", acknowledge=False)
            self.assertEqual(stored_log_bytes(f"{directory}/cp.db"), before)

    def test_new_request_is_accepted_after_previous_prune_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, service = self._make(directory)
            service.request_job_log_prune("2026-03-03", acknowledge=True)
            repository.backfill_job_log_sizes(batch_size=25)
            while not repository.run_job_log_prune_batch(batch_size=1)["complete"]:
                pass
            self.assertEqual(repository.job_log_prune_state()["status"], "complete")
            result = service.request_job_log_prune("2026-02-01", acknowledge=True)
            self.assertTrue(result["created"])
            self.assertEqual(result["status"], "pending")


class JobLogPruneRunTests(unittest.TestCase):
    """S4: bounded resumable prune batches with a durable ordered cursor."""

    def _make(self, directory):
        repository = SQLiteJobRepository(f"{directory}/cp.db")
        service = make_service(repository)
        return repository, service

    def _eligible(self, repository, job_id, **kwargs):
        return repository.save(JobRecord(
            id=job_id, worker_id="worker-a", command="backup.run",
            status=JobStatus.SUCCEEDED, log_lines=[BIG_TEXT],
            log_timestamps=["2024-05-05T10:00:00Z"], result_summary={"status_code": 0},
            **kwargs,
        ))

    def test_batches_remove_logs_in_durable_cursor_order_until_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, _ = self._make(directory)
            base = datetime(2026, 3, 1, 12, 0, 0)
            self._eligible(repository, "a-old", finished_at=base)
            self._eligible(repository, "b-old", finished_at=base + timedelta(days=1))
            self._eligible(repository, "c-after-cutoff", finished_at=base + timedelta(days=9))
            protected = repository.save(JobRecord(
                id="d-diagnostic", worker_id="worker-a", command="backup.run",
                status=JobStatus.FAILED, log_lines=[BIG_TEXT],
                finished_at=base + timedelta(days=1),
                result_summary={"diagnostic_code": "worker_lease_expired"},
            ))
            repository.backfill_job_log_sizes(batch_size=25)
            repository.request_job_log_prune("2026-03-02", now=utcnow())

            first = repository.run_job_log_prune_batch(batch_size=2, now=utcnow())
            self.assertEqual(len(first["removed"]), 2)
            self.assertEqual(first["complete"], False)
            self.assertTrue(first["active"])
            state = repository.job_log_prune_state()
            self.assertEqual(state["processed"], 2)
            self.assertGreater(state["bytes_freed"], 0)

            second = repository.run_job_log_prune_batch(batch_size=2, now=utcnow())
            self.assertEqual(second["removed"], [])
            # The protected synthetic failure never stalls the cursor, though
            # it was processed and is past the cursor.
            self.assertEqual(second["skipped"], ["d-diagnostic"])
            self.assertEqual(second["complete"], True)
            final = repository.job_log_prune_state()
            self.assertEqual(final["status"], "complete")
            self.assertEqual(final["processed"], 3)
            self.assertIsNotNone(final["completed_at"])

            for job_id in ("a-old", "b-old"):
                stored = repository.get(job_id)
                self.assertTrue(getattr(stored, "job_log_removed", False))
                self.assertEqual(stored.result_summary, {"status_code": 0})
            after = repository.get(protected.id)
            self.assertEqual(after.log_lines, [BIG_TEXT])
            self.assertFalse(getattr(after, "job_log_removed", False))
            kept = repository.get("c-after-cutoff")
            self.assertEqual(kept.log_lines, [BIG_TEXT])
            events = repository.job_log_policy_events()
            self.assertTrue(any(event["kind"] == "logs_pruned" for event in events))

            # Idempotent: a further batch finds nothing and stays complete.
            again = repository.run_job_log_prune_batch(batch_size=2, now=utcnow())
            self.assertEqual(again["removed"], [])
            self.assertTrue(again["complete"])

    def test_prune_state_resumes_across_restart_of_the_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/cp.db"
            repository = SQLiteJobRepository(database)
            base = datetime(2026, 3, 1, 12, 0, 0)
            for index in range(6):
                repository.save(JobRecord(
                    id=f"job-{index}", worker_id="worker-a", command="backup.run",
                    status=JobStatus.SUCCEEDED, log_lines=[BIG_TEXT],
                    finished_at=base + timedelta(hours=index),
                ))
            repository.backfill_job_log_sizes(batch_size=25)
            repository.request_job_log_prune("2026-03-02", now=utcnow())
            repository.run_job_log_prune_batch(batch_size=2, now=utcnow())

            restarted = SQLiteJobRepository(database)
            state = restarted.job_log_prune_state()
            self.assertEqual(state["status"], "pending")
            self.assertEqual(state["processed"], 2)
            while not restarted.run_job_log_prune_batch(batch_size=2, now=utcnow())["complete"]:
                pass
            final = restarted.job_log_prune_state()
            self.assertEqual(final["processed"], 6)
            self.assertEqual(final["status"], "complete")
            for job in restarted.list():
                self.assertTrue(getattr(job, "job_log_removed", False))

    def test_lane_prioritizes_pending_prune_and_defers_archive_until_done(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/cp.db"
            repository = SQLiteJobRepository(database)
            service = make_service(repository)
            old_cutoff = utcnow() - timedelta(days=60)
            self._eligible(repository, "a-old", finished_at=old_cutoff)
            between = self._eligible(
                repository,
                "b-between",
                finished_at=utcnow() - timedelta(days=40),
            )
            service.update_settings(
                job_log_maintenance_enabled=True,
                job_log_archive_after_days=30,
                job_log_retention_days=90,
            )
            repository.backfill_job_log_sizes(batch_size=25)
            service.request_job_log_prune(
                (utcnow() - timedelta(days=45)).strftime("%Y-%m-%d"), acknowledge=True
            )
            lane = make_maintenance(service, now_fn=lambda: utcnow(), batch_size=2)
            # Seed enough overdue logs that two bounded batches cannot finish:
            # the prune keeps priority across ticks.
            for index in range(12):
                repository.save(JobRecord(
                    id=f"seed-{index:02d}", worker_id="worker-a", command="backup.run",
                    status=JobStatus.SUCCEEDED, log_lines=["z" * 30],
                    finished_at=old_cutoff + timedelta(minutes=index),
                ))
            with mock.patch.object(time, "sleep", lambda *_: None):
                pending = lane.run_once()
            self.assertIn("prune", pending)
            self.assertEqual(pending["prune"]["state"]["status"], "pending")
            self.assertNotIn("archived", pending["actions"])
            self.assertNotIn("expired", pending["actions"])
            # The job inside the archive range was not compressed by the same
            # tick: the prune had priority for every phase.
            self.assertFalse(getattr(repository.get(between.id), "job_log_removed", False))
            self.assertEqual(repository._policy_metadata_for(between.id)["archived"], 0)

            done = lane.run_once()
            self.assertGreaterEqual(done["actions"]["archived"], 1)

    def test_lane_prune_reaches_legacy_rows_through_bounded_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/cp.db"
            repository = SQLiteJobRepository(database)
            service = make_service(repository)
            repository.save(JobRecord(
                id="legacy-1", worker_id="worker-a", command="backup.run",
                status=JobStatus.SUCCEEDED, log_lines=[BIG_TEXT],
                finished_at=utcnow() - timedelta(days=60),
            ))
            connection = sqlite3.connect(database)
            try:
                connection.execute("DELETE FROM job_log_sizes")
                connection.execute("DROP TRIGGER trg_job_log_sizes_insert")
                connection.execute("DROP TRIGGER trg_job_log_sizes_update")
                connection.commit()
            finally:
                connection.close()
            self.assertFalse(repository.job_log_usage()["complete"])
            service.update_settings(job_log_maintenance_enabled=True, job_log_retention_days=90)
            service.request_job_log_prune(
                (utcnow() - timedelta(days=45)).strftime("%Y-%m-%d"), acknowledge=True
            )
            lane = make_maintenance(service, now_fn=lambda: utcnow())
            with mock.patch.object(time, "sleep", lambda *_: None):
                first = lane.run_once()
                if first["prune"]["state"]["status"] == "pending":
                    lane.run_once()
            state = repository.job_log_prune_state()
            self.assertEqual(state["status"], "complete")
            self.assertEqual(state["processed"], 1)
            self.assertTrue(repository.get("legacy-1").job_log_removed)
            self.assertTrue(repository.job_log_usage()["complete"])

    def test_adversarial_id_keyset_backfill_and_finish_time_cursor_never_skip_rows(self):
        """B1 regression: index discovery order (id keyset) must never lose
        finish-time-older rows behind the cursor. The cursor is not read or
        advanced until the compact backfill is complete."""
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/cp.db"
            repository = SQLiteJobRepository(database)
            june = datetime(2025, 6, 15, 12, 0)
            january = datetime(2025, 1, 10, 12, 0)
            expected_ids = set()
            # Adversarial: id lexical order (q... first) disagrees with
            # finish-time order (January z... rows are the oldest).
            for index in range(1, 29):
                repository.save(JobRecord(
                    id=f"q{index:02d}", worker_id="worker-a", command="backup.run",
                    status=JobStatus.SUCCEEDED, log_lines=[BIG_TEXT],
                    finished_at=june + timedelta(minutes=index),
                ))
                expected_ids.add(f"q{index:02d}")
            for index in range(1, 29):
                repository.save(JobRecord(
                    id=f"z{index:02d}", worker_id="worker-a", command="backup.run",
                    status=JobStatus.SUCCEEDED, log_lines=[BIG_TEXT],
                    finished_at=january,
                ))
                expected_ids.add(f"z{index:02d}")
            # Simulate a legacy database whose compact metadata rows are
            # rediscovered by the bounded id-keyset backfill: the q rows
            # (later finished) surface first.
            connection = sqlite3.connect(database)
            try:
                connection.execute("DELETE FROM job_log_sizes")
                connection.execute("DROP TRIGGER trg_job_log_sizes_insert")
                connection.execute("DROP TRIGGER trg_job_log_sizes_update")
                connection.execute("DROP TRIGGER trg_job_log_sizes_delete")
                connection.commit()
            finally:
                connection.close()
            repository.request_job_log_prune("2025-08-01", now=datetime(2025, 11, 1, 12, 0))

            # While indexing is incomplete the prune must not read or advance
            # the cursor and must not delete anything.
            before = stored_log_bytes(database)
            first = repository.backfill_job_log_sizes(batch_size=28)
            self.assertEqual(first["backfilled"], 28)  # q rows discovered first
            self.assertFalse(repository.job_log_usage()["complete"])
            waiting = repository.run_job_log_prune_batch(
                batch_size=28, now=datetime(2025, 11, 1, 12, 1)
            )
            self.assertEqual(waiting["removed"], [])
            self.assertTrue(waiting["wait_for_indexing"])
            self.assertFalse(waiting["complete"])
            self.assertTrue(waiting["active"])
            self.assertEqual(stored_log_bytes(database), before)
            waiting_state = repository.job_log_prune_state()
            self.assertEqual(waiting_state["status"], "pending")
            self.assertEqual(waiting_state["processed"], 0)

            # Finish the bounded backfill, then resume the prune after a
            # restart (fresh repository instance over the same database).
            while not repository.backfill_job_log_sizes(batch_size=28)["complete"]:
                pass
            self.assertTrue(repository.job_log_usage()["complete"])
            restarted = SQLiteJobRepository(database)
            removed_ids = []
            while True:
                batch = restarted.run_job_log_prune_batch(
                    batch_size=28, now=datetime(2025, 11, 1, 12, 2)
                )
                self.assertTrue(batch["active"])
                removed_ids.extend(batch["removed"])
                if batch["complete"]:
                    break
            self.assertEqual(len(removed_ids), 56)
            self.assertEqual(set(removed_ids), expected_ids)
            # Finish-time order after the restart: the January (z) rows first.
            self.assertTrue(all(job_id.startswith("z") for job_id in removed_ids[:28]))
            for job in restarted.list():
                self.assertTrue(getattr(job, "job_log_removed", False))
                self.assertEqual(job.log_lines, [])

    def test_prune_cutoff_is_strict_before_next_midnight_across_iso_variants(self):
        """A1: the candidate SQL and the wide-row safety check must both use
        strict `finished_at < next UTC midnight`, for the plain stored ISO, the
        exact-boundary form and the `+00:00` / microsecond SQLite variants."""
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/cp.db"
            repository = SQLiteJobRepository(database)
            for job_id, finished_sql in (
                ("before-plain", "2026-03-03T23:59:59"),
                ("before-tz", "2026-03-03T23:59:59+00:00"),
                ("before-micro", "2026-03-03T23:59:59.999999"),
                ("boundary-plain", "2026-03-04T00:00:00"),
                ("boundary-tz", "2026-03-04T00:00:00+00:00"),
                ("boundary-micro", "2026-03-04T00:00:00.000001"),
            ):
                repository.save(JobRecord(
                    id=job_id, worker_id="worker-a", command="backup.run",
                    status=JobStatus.SUCCEEDED, log_lines=[BIG_TEXT],
                    finished_at=datetime(2026, 3, 4, 0, 0, 0),
                ))
            connection = sqlite3.connect(database)
            try:
                for job_id, finished_sql in (
                    ("before-plain", "2026-03-03T23:59:59"),
                    ("before-tz", "2026-03-03T23:59:59+00:00"),
                    ("before-micro", "2026-03-03T23:59:59.999999"),
                    ("boundary-plain", "2026-03-04T00:00:00"),
                    ("boundary-tz", "2026-03-04T00:00:00+00:00"),
                    ("boundary-micro", "2026-03-04T00:00:00.000001"),
                ):
                    connection.execute(
                        "UPDATE jobs SET finished_at = ? WHERE id = ?",
                        (finished_sql, job_id),
                    )
                connection.commit()
            finally:
                connection.close()
            repository.backfill_job_log_sizes(batch_size=10)
            repository.request_job_log_prune("2026-03-03", now=datetime(2026, 3, 4, 1, 0))
            batch = repository.run_job_log_prune_batch(
                batch_size=10, now=datetime(2026, 3, 4, 1, 1)
            )
            self.assertEqual(
                set(batch["removed"]),
                {"before-plain", "before-tz", "before-micro"},
            )
            self.assertEqual(batch["skipped"], [])
            self.assertTrue(batch["complete"])
            for job_id in ("boundary-plain", "boundary-tz", "boundary-micro"):
                stored = repository.get(job_id)
                self.assertEqual(stored.log_lines, [BIG_TEXT])
                self.assertFalse(getattr(stored, "job_log_removed", False))
                self.assertEqual(
                    repository._policy_metadata_for(job_id)["payload_present"], 1
                )

    def test_settings_status_shows_prune_state_and_compact_indexing(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/cp.db"
            repository = SQLiteJobRepository(database)
            service = make_service(repository)
            self._eligible(repository, "a-old", finished_at=utcnow() - timedelta(days=60))
            connection = sqlite3.connect(database)
            try:
                connection.execute("DELETE FROM job_log_sizes")
                connection.execute("DROP TRIGGER trg_job_log_sizes_insert")
                connection.execute("DROP TRIGGER trg_job_log_sizes_update")
                connection.commit()
            finally:
                connection.close()
            requested = service.request_job_log_prune("2001-01-01", acknowledge=True)
            self.assertTrue(requested["created"])
            status = service.job_log_maintenance_status()
            self.assertEqual(status["prune_state"]["cutoff_date"], "2001-01-01")
            self.assertEqual(status["prune_state"]["status"], "pending")
            # Compact indexing state is surfaced for the pending prune.
            self.assertFalse(status["indexing"]["complete"])
            self.assertEqual(status["indexing"]["accounted_rows"], 0)

            repository.backfill_job_log_sizes(batch_size=10)
            status = service.job_log_maintenance_status()
            self.assertTrue(status["indexing"]["complete"])

    def test_lane_first_tick_waits_for_the_normal_cadence_after_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteJobRepository(f"{directory}/cp.db")
            service = make_service(repository)
            executed = threading.Event()

            def slow_execute(now):
                executed.set()
                return {"supported": True, "enabled": False, "actions": {}}

            lane = job_log_maintenance.JobLogMaintenanceService(
                service, interval_seconds=0.2, now_fn=lambda: None,
            )
            with mock.patch.object(lane, "_execute", side_effect=slow_execute):
                lane.start()
                try:
                    self.assertFalse(executed.wait(0.05), "first tick must not run inline at boot")
                    self.assertTrue(executed.wait(3))
                finally:
                    lane.stop()
            self.assertFalse(lane._thread.is_alive())


class IndependentLogControlsTests(unittest.TestCase):
    """S5: automatic archival independent of the retention/quota opt-ins."""

    def _make(self, directory):
        database = f"{directory}/cp.db"
        repository = SQLiteJobRepository(database)
        service = make_service(repository)
        return repository, service

    def _overdue(self, repository, **kwargs):
        return repository.save(JobRecord(
            worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
            log_lines=[BIG_TEXT], log_timestamps=["2024-05-05T10:00:00Z"],
            finished_at=utcnow() - timedelta(days=200), **kwargs,
        ))

    def test_automatic_archive_runs_with_retention_and_quota_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, service = self._make(directory)
            job = self._overdue(repository)
            # Defaults: retention and quota are both opt-ins and stay off.
            defaults = service.update_settings()
            self.assertFalse(defaults.job_log_maintenance_enabled)
            self.assertIsNone(defaults.job_log_quota_gib)
            lane = make_maintenance(service, now_fn=lambda: utcnow())
            with mock.patch.object(time, "sleep", lambda *_: None):
                result = lane.run_once()
            self.assertGreaterEqual(result["actions"]["archived"], 1)
            self.assertEqual(repository._policy_metadata_for(job.id)["archived"], 1)
            self.assertFalse(getattr(repository.get(job.id), "job_log_removed", False))
            self.assertNotIn("expired", result["actions"])
            self.assertNotIn("evicted", result["actions"])
            self.assertFalse(result["quota_breached"])
            self.assertTrue(any(event["kind"] == "logs_archived" for event in repository.job_log_policy_events()))

    def test_quota_only_deletion_runs_when_retention_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, service = self._make(directory)
            job = self._overdue(repository)
            service.update_settings(
                job_log_maintenance_enabled=False,
                job_log_quota_enabled=True,
                job_log_quota_gib=1,
            )
            lane = make_maintenance(service, now_fn=lambda: utcnow())
            with mock.patch(job_log_maintenance.__name__ + ".BYTES_PER_GIB", 10):
                with mock.patch(
                    "src.control_plane.infrastructure.repositories.sqlite.JOB_LOG_QUOTA_GIB_TO_BYTES", 10
                ):
                    with mock.patch.object(time, "sleep", lambda *_: None):
                        result = lane.run_once()
            self.assertTrue(result["quota_breached"])
            self.assertGreaterEqual(result["actions"]["evicted"], 1)
            self.assertTrue(getattr(repository.get(job.id), "job_log_removed", False))
            self.assertNotIn("expired", result["actions"])
            self.assertTrue(any(event["kind"] == "quota_breach" for event in repository.job_log_policy_events()))

    def test_effective_quota_flag_prefers_explicit_value_and_defaults_to_legacy(self):
        self.assertFalse(effective_job_log_quota_enabled(None))
        self.assertFalse(effective_job_log_quota_enabled(SettingsRecord()))
        self.assertTrue(effective_job_log_quota_enabled(SettingsRecord(
            job_log_maintenance_enabled=True, job_log_quota_gib=4,
        )))
        # A dormant legacy quota never activates by itself.
        self.assertFalse(effective_job_log_quota_enabled(SettingsRecord(
            job_log_maintenance_enabled=False, job_log_quota_gib=4,
        )))
        self.assertFalse(effective_job_log_quota_enabled(SettingsRecord(
            job_log_quota_enabled=False, job_log_maintenance_enabled=True, job_log_quota_gib=4,
        )))
        self.assertTrue(effective_job_log_quota_enabled(SettingsRecord(
            job_log_quota_enabled=True, job_log_maintenance_enabled=False, job_log_quota_gib=None,
        )))

    def test_transactional_recheck_uses_explicit_quota_flag_and_retention_toggle_preserves_it(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/cp.db"
            repository = SQLiteJobRepository(database)
            settings = SQLiteSettingsRepository(database)
            service = make_service(repository, settings_repository=settings)
            self._overdue(repository, id="q-old")
            service.update_settings(
                job_log_maintenance_enabled=False,
                job_log_quota_enabled=True,
                job_log_quota_gib=1,
            )
            with mock.patch(
                "src.control_plane.infrastructure.repositories.sqlite.JOB_LOG_QUOTA_GIB_TO_BYTES", 10
            ):
                # The stored recheck honors the explicit flag even though the
                # legacy maintenance opt-in is disabled.
                result = repository.evict_job_logs_for_quota(free_to_bytes=0)
            self.assertEqual(result["evicted"], 1)

            settings = SQLiteSettingsRepository(database)
            # Toggling retention later preserves the prior explicit quota state.
            service.update_settings(job_log_maintenance_enabled=True)
            self.assertTrue(settings.get().job_log_quota_enabled)
            service.update_settings(job_log_maintenance_enabled=False)
            self.assertTrue(settings.get().job_log_quota_enabled)

    def test_presence_aware_migration_activates_only_previously_active_legacy_quota(self):
        legacy_columns = (
            "id TEXT PRIMARY KEY, restic_repository_base TEXT NOT NULL DEFAULT '', "
            "restic_password_secret_id TEXT, rclone_conf_secret_id TEXT, "
            "global_cron_expression TEXT, control_plane_public_url TEXT NOT NULL DEFAULT '', "
            "snapshot_explorer_listing_max_output_bytes INTEGER, "
            "job_log_maintenance_enabled INTEGER NOT NULL DEFAULT 0, "
            "job_log_archive_after_days INTEGER NOT NULL DEFAULT 30, "
            "job_log_retention_days INTEGER NOT NULL DEFAULT 90, "
            "job_log_quota_gib INTEGER, updated_at TEXT NOT NULL"
        )
        for enabled, quota, expected_flag in ((True, 8, True), (False, 5, False), (True, None, False)):
            with self.subTest(enabled=enabled, quota=quota):
                with tempfile.TemporaryDirectory() as directory:
                    database = f"{directory}/cp.db"
                    connection = sqlite3.connect(database)
                    try:
                        connection.execute(f"CREATE TABLE settings ({legacy_columns})")
                        connection.execute(
                            "INSERT INTO settings (id, job_log_maintenance_enabled, job_log_quota_gib, updated_at) "
                            "VALUES ('default', ?, ?, ?)",
                            (1 if enabled else 0, quota, utcnow().isoformat()),
                        )
                        connection.commit()
                    finally:
                        connection.close()
                    stored = SQLiteSettingsRepository(database).get()
                    self.assertEqual(stored.job_log_quota_enabled, expected_flag)

    def test_update_settings_validates_quota_enabled_and_roundtrips_it(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/cp.db"
            repository = SQLiteJobRepository(database)
            settings = SQLiteSettingsRepository(database)
            service = make_service(repository, settings_repository=settings)

            default = service.update_settings()
            self.assertIsNone(default.job_log_quota_enabled)

            enabled = service.update_settings(job_log_quota_enabled=True)
            self.assertTrue(enabled.job_log_quota_enabled)
            explicit = service.update_settings(job_log_quota_enabled=False)
            self.assertFalse(explicit.job_log_quota_enabled)
            # An omitted PATCH key never unsets the flag.
            omitted = service.update_settings(job_log_quota_gib=SETTINGS_UNSET)
            self.assertFalse(omitted.job_log_quota_enabled)

            for invalid in ("yes", 1, 0, 1.5):
                with self.subTest(value=invalid):
                    with self.assertRaises(ValueError):
                        service.update_settings(job_log_quota_enabled=invalid)
            with self.assertRaises(ValueError):
                service.update_settings(job_log_quota_enabled=SETTINGS_UNSET)

            saved = settings.get()
            self.assertFalse(saved.job_log_quota_enabled)
            saved.job_log_quota_enabled = True
            settings.save(saved)
            self.assertTrue(SQLiteSettingsRepository(database).get().job_log_quota_enabled)


if __name__ == "__main__":
    unittest.main()
