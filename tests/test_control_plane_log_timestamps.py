import sqlite3
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

import brotli
from cryptography.fernet import Fernet

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.application.services.job_event_broker import JobEventBroker
from src.control_plane.domain.models import JobRecord, JobStatus, WorkerRecord, utcnow
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
from src.control_plane.infrastructure.repositories.sqlite import SQLiteJobRepository
from src.control_plane.infrastructure.security.secret_codec import SecretCodec


SOURCE_TIME = "2024-05-05T10:00:00.123456789Z"
TERMINAL_TIME = "2024-05-05T10:00:01Z"


class ControlPlaneLogTimestampTests(unittest.TestCase):
    @staticmethod
    def make_service(job_repository, broker=None):
        workers = InMemoryWorkerRepository()
        workers.save(WorkerRecord(name="worker-a", host_name="host", id="worker-a", status="online", last_seen_at=utcnow()))
        targets = InMemoryTargetRepository()
        return ControlPlaneService(
            worker_repository=workers,
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=targets,
            job_repository=job_repository,
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=SecretCodec(Fernet.generate_key()),
            settings_repository=InMemorySettingsRepository(),
            job_event_broker=broker,
        )

    def test_archived_job_logs_keep_the_public_view_and_reject_ineligible_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database)
            service = self.make_service(repository)
            now = utcnow()
            cutoff = now - timedelta(days=30)
            line = "Backup progress: copied café.txt to disk. " * 100
            job = JobRecord(
                worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
                log_lines=[line], log_timestamps=[SOURCE_TIME],
                finished_at=cutoff, result_summary={"status_code": 0},
            )
            repository.save(job)
            before = service.get_job_view(job.id)
            self.assertEqual(before["log_lines"], [line])
            self.assertEqual(before["log_timestamps"], [SOURCE_TIME])

            self.assertGreater(repository.archive_job_log(job.id, cutoff), 0)
            self.assertEqual(repository.archive_job_log(job.id, cutoff), 0)
            reopened = self.make_service(SQLiteJobRepository(database))
            self.assertEqual(reopened.get_job_view(job.id), before)

            for status, finished_at, summary in (
                (JobStatus.SUCCEEDED, now, None),
                (JobStatus.IN_PROGRESS, cutoff, None),
                (JobStatus.FAILED, cutoff, {"diagnostic_code": "worker_lease_expired"}),
            ):
                with self.subTest(status=status):
                    untouched = repository.save(JobRecord(
                        worker_id="worker-a", command="backup.run", status=status,
                        log_lines=[line], finished_at=finished_at, result_summary=summary,
                    ))
                    self.assertEqual(repository.archive_job_log(untouched.id, cutoff), 0)
                    self.assertEqual(repository.get(untouched.id), untouched)

    def test_archived_log_timestamps_stay_aligned_for_partial_and_all_null_values(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database)
            service = self.make_service(repository)
            cutoff = utcnow() - timedelta(days=1)
            mixed_lines = ["Backup progress: copied text. " * 40, "long line without source time " * 40]
            mixed = repository.save(JobRecord(
                worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
                log_lines=mixed_lines, log_timestamps=[SOURCE_TIME, None],
                finished_at=cutoff, result_summary={"status_code": 0},
            ))
            all_none = repository.save(JobRecord(
                worker_id="worker-a", command="backup.run", status=JobStatus.CANCELED,
                log_lines=["plain line " * 40], log_timestamps=[None],
                finished_at=cutoff, result_summary={"status_code": 1},
            ))
            mixed_before = service.get_job_view(mixed.id)
            none_before = service.get_job_view(all_none.id)

            self.assertGreater(repository.archive_job_log(mixed.id, cutoff), 0)
            self.assertGreater(repository.archive_job_log(all_none.id, cutoff), 0)

            reopened = self.make_service(SQLiteJobRepository(database))
            self.assertEqual(reopened.get_job_view(mixed.id), mixed_before)
            self.assertEqual(reopened.get_job_view(mixed.id)["log_timestamps"], [SOURCE_TIME, None])
            self.assertEqual(reopened.get_job_view(all_none.id), none_before)
            self.assertNotIn("log_timestamps", reopened.get_job_view(all_none.id))
            self.assertIsNone(repository.get(all_none.id).log_timestamps)
            # Bulk reads decode the archive transparently, contract unchanged.
            self.assertEqual(
                {job.id: job.log_lines for job in repository.list()},
                {mixed.id: mixed_lines, all_none.id: ["plain line " * 40]},
            )

    def test_archive_job_log_treats_legacy_cancelled_as_archivable_but_not_unconfirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database)
            service = self.make_service(repository)
            cutoff = utcnow() - timedelta(days=1)
            legacy = repository.save(JobRecord(
                worker_id="worker-a", command="backup.run", status=JobStatus.CANCELED,
                log_lines=["legacy line " * 40], finished_at=cutoff, result_summary={"status_code": 1},
            ))
            unconfirmed = repository.save(JobRecord(
                worker_id="worker-a", command="backup.run", status=JobStatus.UNCONFIRMED,
                log_lines=["legacy line " * 40], finished_at=cutoff, result_summary={"status_code": 1},
            ))
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ?",
                    (utcnow().isoformat(), legacy.id),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertGreater(repository.archive_job_log(legacy.id, cutoff), 0)
            self.assertEqual(repository.archive_job_log(unconfirmed.id, cutoff), 0)

            reopened = self.make_service(SQLiteJobRepository(database))
            self.assertEqual(reopened.get_job_view(legacy.id)["log_lines"], ["legacy line " * 40])
            self.assertEqual(reopened.get_job_view(unconfirmed.id)["log_lines"], ["legacy line " * 40])
            reopened_repository = SQLiteJobRepository(database)
            self.assertEqual(reopened_repository.get(legacy.id).status, JobStatus.CANCELED)
            self.assertEqual(reopened_repository.get(unconfirmed.id).status, JobStatus.UNCONFIRMED)

    def test_archived_log_compression_cannot_drop_a_newer_write_that_lands_during_compression(self):
        with tempfile.TemporaryDirectory() as directory:
            database = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database)
            service = self.make_service(repository)
            cutoff = utcnow() - timedelta(days=1)
            job = repository.save(JobRecord(
                worker_id="worker-a", command="backup.run", status=JobStatus.SUCCEEDED,
                log_lines=["old log line " * 40], finished_at=cutoff, result_summary={"status_code": 0},
            ))
            newer_lines = ["newer log line " * 40]
            newer_job = JobRecord(
                id=job.id, worker_id="worker-a", command="backup.run",
                status=JobStatus.SUCCEEDED, log_lines=newer_lines,
                finished_at=cutoff, result_summary={"status_code": 0},
            )
            original_compress = brotli.compress

            def compress_with_landed_write(data, **kwargs):
                # A competing writer lands between the lock-free snapshot read
                # and the compare-and-swap; the archive must reject it whole.
                repository.save(newer_job)
                return original_compress(data, **kwargs)

            with mock.patch.object(brotli, "compress", side_effect=compress_with_landed_write):
                self.assertEqual(repository.archive_job_log(job.id, cutoff), 0)
            self.assertEqual(repository.get(job.id).log_lines, newer_lines)
            connection = sqlite3.connect(database)
            try:
                stored = connection.execute(
                    "SELECT log_lines_json, job_log_brotli FROM jobs WHERE id = ?", (job.id,)
                ).fetchone()
            finally:
                connection.close()
            self.assertNotEqual(stored[0], "[]")
            self.assertIsNone(stored[1])

            self.assertGreater(repository.archive_job_log(job.id, cutoff), 0)
            reopened = self.make_service(SQLiteJobRepository(database))
            self.assertEqual(reopened.get_job_view(job.id)["log_lines"], newer_lines)

    def test_progress_and_terminal_timestamps_persist_and_reach_public_events(self):
        with tempfile.TemporaryDirectory() as directory:
            repositories = [
                ("memory", InMemoryJobRepository(), None),
                ("sqlite", SQLiteJobRepository(f"{directory}/control-plane.db"), f"{directory}/control-plane.db"),
            ]
            for label, repository, database_path in repositories:
                with self.subTest(repository=label):
                    broker = JobEventBroker()
                    service = self.make_service(repository, broker)
                    job = service.dispatch_job("worker-a", "backup.run")
                    subscription = broker.subscribe(job.id)
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    subscription.get(timeout=1)  # Claim event.

                    progress = service.update_job_progress(
                        "worker-a",
                        job.id,
                        sequence=1,
                        progress={"phase": "backup"},
                        log_lines=["source line", "line without source time"],
                        lease_token=claimed.lease_token,
                        log_timestamps=[SOURCE_TIME, None],
                    )
                    progress_view = service.get_job_view(job.id)
                    self.assertEqual(progress.log_lines, ["source line", "line without source time"])
                    self.assertEqual(progress_view["log_lines"], ["source line", "line without source time"])
                    self.assertEqual(progress_view["log_timestamps"], [SOURCE_TIME, None])
                    progress_event = subscription.get(timeout=1)
                    self.assertEqual(progress_event["log_timestamps"], [SOURCE_TIME, None])

                    service.update_job_status(
                        "worker-a",
                        job.id,
                        JobStatus.SUCCEEDED,
                        result_summary={"state": "done"},
                        log_lines=["terminal line"],
                        lease_token=claimed.lease_token,
                        log_timestamps=[TERMINAL_TIME],
                    )
                    terminal_view = service.get_job_view(job.id)
                    self.assertEqual(
                        terminal_view["log_lines"],
                        ["source line", "line without source time", "terminal line"],
                    )
                    self.assertEqual(
                        terminal_view["log_timestamps"],
                        [SOURCE_TIME, None, TERMINAL_TIME],
                    )
                    terminal_event = subscription.get(timeout=1)
                    self.assertEqual(terminal_event["log_timestamps"], [SOURCE_TIME, None, TERMINAL_TIME])
                    duplicate = service.update_job_status(
                        "worker-a", job.id, JobStatus.SUCCEEDED,
                        result_summary={"state": "done"},
                        log_lines=["terminal line"],
                        lease_token=claimed.lease_token,
                        log_timestamps=[TERMINAL_TIME],
                    )
                    self.assertEqual(duplicate.log_lines, terminal_view["log_lines"])
                    with self.assertRaisesRegex(ValueError, "not in progress"):
                        service.update_job_status(
                            "worker-a", job.id, JobStatus.SUCCEEDED,
                            result_summary={"state": "done"},
                            log_lines=["terminal line"],
                            lease_token=claimed.lease_token,
                            log_timestamps=["2024-05-05T10:00:02Z"],
                        )
                    self.assertEqual(service.get_job_view(job.id), terminal_view)

                    if database_path:
                        reopened = SQLiteJobRepository(database_path)
                        restarted_service = self.make_service(reopened)
                        self.assertEqual(
                            restarted_service.get_job_view(job.id)["log_timestamps"],
                            [SOURCE_TIME, None, TERMINAL_TIME],
                        )
                    subscription.close()

    def test_timestamp_alignment_survives_line_limit_trimming(self):
        with tempfile.TemporaryDirectory() as directory:
            repositories = [
                ("memory", InMemoryJobRepository()),
                ("sqlite", SQLiteJobRepository(f"{directory}/control-plane.db")),
            ]
            for label, repository in repositories:
                with self.subTest(repository=label):
                    service = self.make_service(repository)
                    job = service.dispatch_job("worker-a", "backup.run")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    all_lines = []
                    all_timestamps = []
                    for sequence in range(1, 12):
                        first = (sequence - 1) * 100
                        lines = [f"line-{index:04d}" for index in range(first, first + 100)]
                        timestamps = [
                            f"2024-05-05T10:{index // 60:02d}:{index % 60:02d}Z"
                            for index in range(first, first + 100)
                        ]
                        service.update_job_progress(
                            "worker-a", job.id, sequence, {"phase": "backup"}, lines,
                            lease_token=claimed.lease_token,
                            log_timestamps=timestamps,
                        )
                        all_lines.extend(lines)
                        all_timestamps.extend(timestamps)

                    view = service.get_job_view(job.id)
                    self.assertEqual(view["log_lines"], all_lines[-1000:])
                    self.assertEqual(view["log_timestamps"], all_timestamps[-1000:])

    def test_timestamp_alignment_survives_character_budget_trimming(self):
        with tempfile.TemporaryDirectory() as directory:
            repositories = [
                ("memory", InMemoryJobRepository()),
                ("sqlite", SQLiteJobRepository(f"{directory}/control-plane.db")),
            ]
            for label, repository in repositories:
                with self.subTest(repository=label):
                    service = self.make_service(repository)
                    job = service.dispatch_job("worker-a", "backup.run")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    expected_lines = []
                    expected_timestamps = []
                    sequence = 0
                    for first in range(0, 70, 7):
                        sequence += 1
                        lines = [f"{index:04d}" + "x" * 8188 for index in range(first, first + 7)]
                        timestamps = [
                            f"2024-05-05T10:{index // 60:02d}:{index % 60:02d}Z"
                            for index in range(first, first + 7)
                        ]
                        service.update_job_progress(
                            "worker-a", job.id, sequence, {"phase": "backup"}, lines,
                            lease_token=claimed.lease_token,
                            log_timestamps=timestamps,
                        )
                        expected_lines.extend(lines)
                        expected_timestamps.extend(timestamps)
                    while sum(map(len, expected_lines)) > 512 * 1024:
                        expected_lines.pop(0)
                        expected_timestamps.pop(0)

                    view = service.get_job_view(job.id)
                    self.assertEqual(view["log_lines"], expected_lines)
                    self.assertEqual(view["log_timestamps"], expected_timestamps)

    def test_invalid_timestamps_leave_progress_and_terminal_state_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            repositories = [
                ("memory", InMemoryJobRepository()),
                ("sqlite", SQLiteJobRepository(f"{directory}/control-plane.db")),
            ]
            invalid_values = (
                [SOURCE_TIME, None],  # Too many entries for one line.
                ["x" * 65],  # Timestamp text exceeds the bound.
                [123],  # Timestamp entry is not text or null.
                ["2024-05-05T10:00:00Z\n"],  # Control characters are rejected.
            )
            for label, repository in repositories:
                with self.subTest(repository=label):
                    service = self.make_service(repository)
                    job = service.dispatch_job("worker-a", "backup.run")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    service.update_job_progress(
                        "worker-a", job.id, 1, {"phase": "backup"}, ["accepted"],
                        lease_token=claimed.lease_token,
                        log_timestamps=[SOURCE_TIME],
                    )
                    before = service.get_job_view(job.id)

                    for invalid_timestamps in invalid_values:
                        with self.subTest(timestamps=repr(invalid_timestamps)):
                            with self.assertRaisesRegex(ValueError, "log_timestamps"):
                                service.update_job_progress(
                                    "worker-a", job.id, 2, {"phase": "changed"}, ["rejected"],
                                    lease_token=claimed.lease_token,
                                    log_timestamps=invalid_timestamps,
                                )
                            self.assertEqual(service.get_job_view(job.id), before)
                            stored = service.get_job(job.id)
                            self.assertEqual(stored.result_summary["progress_sequence"], 1)
                            self.assertEqual(stored.log_timestamps, [SOURCE_TIME])

                    with self.assertRaisesRegex(ValueError, "log_timestamps"):
                        service.update_job_status(
                            "worker-a", job.id, JobStatus.SUCCEEDED,
                            result_summary={"state": "must-not-save"},
                            log_lines=["rejected terminal"],
                            lease_token=claimed.lease_token,
                            log_timestamps=["x" * 65],
                        )
                    self.assertEqual(service.get_job_view(job.id), before)
                    stored = service.get_job(job.id)
                    self.assertEqual(stored.status, JobStatus.IN_PROGRESS)
                    self.assertEqual(stored.log_timestamps, [SOURCE_TIME])

    def test_terminal_report_retry_treats_all_missing_timestamps_as_omitted(self):
        for log_lines, first_timestamps in (([], []), (["line without source time"], [None])):
            with self.subTest(log_lines=log_lines):
                service = self.make_service(InMemoryJobRepository())
                job = service.dispatch_job("worker-a", "backup.run")
                claimed = service.fetch_jobs_for_worker("worker-a")[0]
                service.update_job_status(
                    "worker-a", job.id, JobStatus.SUCCEEDED,
                    result_summary={"state": "done"}, log_lines=log_lines,
                    lease_token=claimed.lease_token, log_timestamps=first_timestamps,
                )
                before = service.get_job_view(job.id)

                service.update_job_status(
                    "worker-a", job.id, JobStatus.SUCCEEDED,
                    result_summary={"state": "done"}, log_lines=log_lines,
                    lease_token=claimed.lease_token,
                )

                self.assertEqual(service.get_job_view(job.id), before)

    def test_legacy_progress_without_source_timestamps_does_not_expose_fabricated_values(self):
        service = self.make_service(InMemoryJobRepository())
        job = service.dispatch_job("worker-a", "backup.run")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]

        service.update_job_progress(
            "worker-a", job.id, 1, {"phase": "backup"}, ["plain line"],
            lease_token=claimed.lease_token,
        )

        view = service.get_job_view(job.id)
        self.assertEqual(view["log_lines"], ["plain line"])
        self.assertNotIn("log_timestamps", view)
        self.assertIsNone(service.get_job(job.id).log_timestamps)

    def test_existing_sqlite_jobs_migrate_without_changing_logs_or_schema_version(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = f"{directory}/control-plane.db"
            repository = SQLiteJobRepository(database_path)
            from src.control_plane.domain.models import JobRecord
            repository.save(JobRecord(worker_id="worker-a", command="backup.run", id="legacy-job", log_lines=["old line"]))

            connection = sqlite3.connect(database_path)
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
                if "log_timestamps_json" in columns:
                    connection.execute("ALTER TABLE jobs DROP COLUMN log_timestamps_json")
                connection.commit()
            finally:
                connection.close()

            migrated = SQLiteJobRepository(database_path)
            job = migrated.get("legacy-job")
            connection = sqlite3.connect(database_path)
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            finally:
                connection.close()

            self.assertIn("log_timestamps_json", columns)
            self.assertEqual(job.log_lines, ["old line"])
            self.assertIsNone(job.log_timestamps)
            self.assertEqual(migrated.get_schema_version(), 1)
            self.assertEqual(migrated.get("legacy-job").log_lines, ["old line"])


if __name__ == "__main__":
    unittest.main()
