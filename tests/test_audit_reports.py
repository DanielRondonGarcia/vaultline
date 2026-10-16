import json
import tempfile
import unittest
from datetime import datetime, timedelta
from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.domain.models import (
    BackupTargetRecord,
    JobRecord,
    JobStatus,
    RetentionPolicyRecord,
    SettingsRecord,
    StorageProfileRecord,
    WorkerRecord,
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
from src.control_plane.infrastructure.repositories.sqlite import SQLiteJobRepository


class BackupAuditReportTests(unittest.TestCase):
    @staticmethod
    def _job(job_id, started_at, *, command="backup.run", status=JobStatus.SUCCEEDED, target_id="target-a", worker_id="worker-a", result_summary=None):
        terminal = status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED, "cancelled")
        return JobRecord(
            id=job_id,
            worker_id=worker_id,
            command=command,
            target_id=target_id,
            status=status,
            submitted_at=started_at,
            started_at=started_at,
            finished_at=started_at + timedelta(minutes=2) if terminal else None,
            result_summary=result_summary,
        )

    @staticmethod
    def _service(job_repository):
        workers = InMemoryWorkerRepository()
        workers.save(WorkerRecord(id="worker-a", name="worker-a", host_name="server-a"))
        workers.save(WorkerRecord(id="worker-b", name="worker-b", host_name="server-b"))
        targets = InMemoryTargetRepository()
        targets.save(BackupTargetRecord(
            id="target-a",
            name="service-a",
            worker_id="worker-a",
            compose_project="project-a",
            volume_targets=["database:/var/lib/database"],
            backup_strategy="restic",
            backup_mode="cold",
            runtime_environment={"RESTIC_PASSWORD": "PRIVATE_TEST_SECRET"},
            restic_password_secret_id="secret-a",
            cron_expression="0 2 * * *",
            retention_policy_id="retention-a",
        ))
        targets.save(BackupTargetRecord(
            id="target-b",
            name="service-b",
            worker_id="worker-b",
            backup_strategy="restic",
        ))
        retentions = InMemoryRetentionPolicyRepository()
        retentions.save(RetentionPolicyRecord(id="retention-a", name="daily-seven", keep_daily=7))
        return ControlPlaneService(
            worker_repository=workers,
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=targets,
            job_repository=job_repository,
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=retentions,
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=None,
            settings_repository=InMemorySettingsRepository(),
        )

    def test_report_uses_inclusive_scheduler_dates_and_completed_backup_percentages(self):
        repository = InMemoryJobRepository()
        start = datetime(2026, 3, 8, 5, 0, 0)
        end_exclusive = datetime(2026, 3, 9, 4, 0, 0)
        for job in (
            self._job("before", start - timedelta(seconds=1)),
            self._job("start-success", start),
            self._job("end-failure", end_exclusive - timedelta(seconds=1), status=JobStatus.FAILED),
            self._job("at-end", end_exclusive, status=JobStatus.FAILED),
            self._job("cancelled", start + timedelta(hours=2), status="cancelled"),
            self._job("running", start + timedelta(hours=3), status=JobStatus.IN_PROGRESS),
            self._job("restore", start + timedelta(hours=4), command="restore.run"),
        ):
            repository.save(job)

        service = self._service(repository)
        service.settings_repository.save(SettingsRecord(global_cron_expression="15 3 * * *"))
        report = service.backup_audit_report(
            start_date="2026-03-08",
            end_date="2026-03-08",
            timezone_name="America/New_York",
        )

        self.assertEqual(report["period"], {
            "start_date": "2026-03-08",
            "end_date": "2026-03-08",
            "timezone": "America/New_York",
        })
        self.assertEqual(report["summary"], {
            "target_count": 2,
            "backup_job_count": 4,
            "completed_job_count": 2,
            "succeeded_count": 1,
            "failed_count": 1,
            "other_status_counts": {"canceled": 1, "in_progress": 1},
            "unmapped_target_job_count": 0,
            "succeeded_percent": 50.0,
            "failed_percent": 50.0,
            "backup_size": {
                "bytes_processed": None,
                "bytes_added": None,
                "coverage": {
                    "bytes_processed": {"reported_jobs": 0, "unavailable_jobs": 4},
                    "bytes_added": {"reported_jobs": 0, "unavailable_jobs": 4},
                },
            },
        })
        matrix = {row["target_id"]: row for row in report["matrix"]}
        target_a = matrix["target-a"]
        self.assertEqual(target_a["service"], "service-a")
        self.assertEqual(target_a["server"], {"id": "worker-a", "name": "worker-a", "host": "server-a"})
        self.assertEqual(target_a["resources"], {
            "compose_project": "project-a",
            "volumes": ["database:/var/lib/database"],
            "filesystem_paths": [],
            "pvc_names": [],
        })
        self.assertEqual(target_a["schedule"], {
            "cron_expression": "0 2 * * *",
            "source": "target",
            "timezone": "America/New_York",
        })
        self.assertEqual(target_a["retention"]["name"], "daily-seven")
        self.assertEqual(target_a["retention"]["keep_daily"], 7)
        self.assertEqual(target_a["job_counts"]["other_statuses"], {"canceled": 1, "in_progress": 1})
        self.assertEqual((target_a["succeeded_percent"], target_a["failed_percent"]), (50.0, 50.0))
        target_b = matrix["target-b"]
        self.assertEqual(target_b["job_counts"]["total"], 0)
        self.assertIsNone(target_b["succeeded_percent"])
        self.assertIsNone(target_b["failed_percent"])
        self.assertEqual(target_b["schedule"], {
            "cron_expression": "15 3 * * *",
            "source": "global",
            "timezone": "America/New_York",
        })
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("PRIVATE_TEST_SECRET", serialized)
        self.assertNotIn("secret-a", serialized)
        self.assertNotIn("runtime_environment", serialized)

    def test_report_rejects_reversed_dates_without_mutating_job_history(self):
        repository = InMemoryJobRepository()
        repository.save(self._job("existing", datetime(2026, 3, 8, 5, 0, 0)))
        before = [job.id for job in repository.list()]

        with self.assertRaisesRegex(ValueError, "start_date must be on or before end_date"):
            self._service(repository).backup_audit_report(
                start_date="2026-03-09",
                end_date="2026-03-08",
                timezone_name="America/New_York",
            )

        self.assertEqual([job.id for job in repository.list()], before)

    def test_report_includes_safe_storage_location_and_backup_size_availability_basis(self):
        repository = InMemoryJobRepository()
        service = self._service(repository)
        service.storage_profile_repository.save(StorageProfileRecord(
            id="profile-a",
            name="prod-s3",
            backend_type="s3",
            environment={"AWS_ACCESS_KEY_ID": "AKIATEST", "AWS_SECRET_ACCESS_KEY": "topsecret"},
            secret_refs={"password": "secret-b"},
        ))
        service.target_repository.save(BackupTargetRecord(
            id="target-a",
            name="service-a",
            worker_id="worker-a",
            backup_strategy="restic",
            storage_profile_id="profile-a",
            path_storage="svc/backups",
            retention_policy_id="retention-a",
        ))

        report = service.backup_audit_report(
            start_date="2026-03-08",
            end_date="2026-03-08",
            timezone_name="America/New_York",
        )

        matrix = {row["target_id"]: row for row in report["matrix"]}
        target_a = matrix["target-a"]
        self.assertEqual(target_a["storage_location"], {
            "backend_type": "s3",
            "storage_profile_name": "prod-s3",
            "path_storage": "svc/backups",
        })
        # No backup.run jobs in the period: both metrics are null (not zero)
        # and coverage reports zero reported jobs.
        self.assertEqual(target_a["backup_size"]["bytes_processed"], None)
        self.assertEqual(target_a["backup_size"]["bytes_added"], None)
        self.assertEqual(target_a["backup_size"]["coverage"]["bytes_processed"], {"reported_jobs": 0, "unavailable_jobs": 0})
        self.assertEqual(target_a["backup_size"]["coverage"]["bytes_added"], {"reported_jobs": 0, "unavailable_jobs": 0})
        availability = report["availability"]
        self.assertFalse(availability["backup_size"]["available"])
        self.assertFalse(availability["backup_size"]["metrics"]["bytes_processed"]["available"])
        self.assertFalse(availability["backup_size"]["metrics"]["bytes_added"]["available"])
        self.assertIn("total_bytes_processed", availability["backup_size"]["basis"])
        self.assertIn("data_added", availability["backup_size"]["basis"])
        self.assertTrue(availability["storage_location"]["available"])

        serialized = json.dumps(report, ensure_ascii=False)
        for forbidden in ("topsecret", "AKIATEST", "secret-b", "AWS_SECRET_ACCESS_KEY", "environment", "secret_refs"):
            self.assertNotIn(forbidden, serialized)

    def test_report_aggregates_backup_size_metrics_with_coverage_and_null_when_unreported(self):
        repository = InMemoryJobRepository()
        base = datetime(2026, 3, 8, 6, 0, 0)
        for job in (
            self._job("j1", base, result_summary={"backup_bytes_processed": 1000, "backup_bytes_added": 400}),
            self._job("j2", base + timedelta(minutes=1), result_summary={"backup_bytes_processed": 2000, "backup_bytes_added": 600}),
            self._job("j3", base + timedelta(minutes=2), result_summary={"backup_bytes_processed": 500}),
            self._job("j4", base + timedelta(minutes=3), result_summary={"progress": {"percent_done": 100.0}}),
            self._job("j5", base + timedelta(minutes=4), status=JobStatus.FAILED, result_summary={"backup_bytes_added": 100}),
            self._job("j6", base + timedelta(minutes=5), result_summary={"backup_bytes_processed": "oops", "backup_bytes_added": True}),
        ):
            repository.save(job)

        report = self._service(repository).backup_audit_report(
            start_date="2026-03-08",
            end_date="2026-03-08",
            timezone_name="America/New_York",
        )

        matrix = {row["target_id"]: row for row in report["matrix"]}
        target_a = matrix["target-a"]
        # Three jobs reported bytes_processed (j1, j2, j3); three did not (j4, j5, j6).
        self.assertEqual(target_a["backup_size"]["bytes_processed"], 3500)
        self.assertEqual(target_a["backup_size"]["coverage"]["bytes_processed"], {"reported_jobs": 3, "unavailable_jobs": 3})
        # Three jobs reported bytes_added (j1, j2, j5); three did not (j3, j4, j6).
        self.assertEqual(target_a["backup_size"]["bytes_added"], 1100)
        self.assertEqual(target_a["backup_size"]["coverage"]["bytes_added"], {"reported_jobs": 3, "unavailable_jobs": 3})
        # Summary aggregates across all in-period backup.run jobs.
        self.assertEqual(report["summary"]["backup_size"]["bytes_processed"], 3500)
        self.assertEqual(report["summary"]["backup_size"]["bytes_added"], 1100)
        self.assertEqual(report["summary"]["backup_size"]["coverage"]["bytes_processed"], {"reported_jobs": 3, "unavailable_jobs": 3})
        self.assertEqual(report["summary"]["backup_size"]["coverage"]["bytes_added"], {"reported_jobs": 3, "unavailable_jobs": 3})

    def test_report_uses_null_not_zero_when_no_job_reported_a_metric(self):
        repository = InMemoryJobRepository()
        base = datetime(2026, 3, 8, 6, 0, 0)
        repository.save(self._job("j1", base, result_summary={"backup_bytes_processed": 1000}))
        repository.save(self._job("j2", base + timedelta(minutes=1), result_summary={"backup_bytes_processed": 2000}))

        report = self._service(repository).backup_audit_report(
            start_date="2026-03-08",
            end_date="2026-03-08",
            timezone_name="America/New_York",
        )

        matrix = {row["target_id"]: row for row in report["matrix"]}
        target_a = matrix["target-a"]
        self.assertEqual(target_a["backup_size"]["bytes_processed"], 3000)
        # No job reported bytes_added: null, not zero, with explicit coverage.
        self.assertIsNone(target_a["backup_size"]["bytes_added"])
        self.assertEqual(target_a["backup_size"]["coverage"]["bytes_added"], {"reported_jobs": 0, "unavailable_jobs": 2})
        self.assertIsNone(report["summary"]["backup_size"]["bytes_added"])
        self.assertEqual(report["summary"]["backup_size"]["coverage"]["bytes_added"], {"reported_jobs": 0, "unavailable_jobs": 2})
        size_availability = report["availability"]["backup_size"]
        self.assertTrue(size_availability["available"])
        self.assertTrue(size_availability["metrics"]["bytes_processed"]["available"])
        self.assertFalse(size_availability["metrics"]["bytes_added"]["available"])
        self.assertEqual(size_availability["metrics"]["bytes_processed"]["reported_jobs"], 2)
        self.assertEqual(size_availability["metrics"]["bytes_added"]["reported_jobs"], 0)

    def test_report_does_not_expose_unrelated_job_result_summary_fields(self):
        repository = InMemoryJobRepository()
        repository.save(self._job(
            "with-private-result-fields",
            datetime(2026, 3, 8, 6, 0, 0),
            result_summary={
                "backup_bytes_processed": 4096,
                "backup_bytes_added": 1024,
                "repository_url": "s3://user:password@example.invalid/private",
                "runtime_environment": {"AWS_SECRET_ACCESS_KEY": "PRIVATE_JOB_RESULT_SECRET"},
                "private_debug": "PRIVATE_DEBUG_VALUE",
            },
        ))

        report = self._service(repository).backup_audit_report(
            start_date="2026-03-08",
            end_date="2026-03-08",
            timezone_name="America/New_York",
        )

        serialized = json.dumps(report, ensure_ascii=False)
        self.assertIn('"bytes_processed": 4096', serialized)
        self.assertIn('"bytes_added": 1024', serialized)
        for forbidden in (
            "user:password",
            "PRIVATE_JOB_RESULT_SECRET",
            "PRIVATE_DEBUG_VALUE",
            "AWS_SECRET_ACCESS_KEY",
            "runtime_environment",
            "repository_url",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_report_preserves_reported_zero_byte_metrics_as_available(self):
        repository = InMemoryJobRepository()
        repository.save(self._job(
            "zero-size",
            datetime(2026, 3, 8, 6, 0, 0),
            result_summary={"backup_bytes_processed": 0, "backup_bytes_added": 0},
        ))

        report = self._service(repository).backup_audit_report(
            start_date="2026-03-08",
            end_date="2026-03-08",
            timezone_name="America/New_York",
        )

        target = next(row for row in report["matrix"] if row["target_id"] == "target-a")
        self.assertEqual(target["backup_size"]["bytes_processed"], 0)
        self.assertEqual(target["backup_size"]["bytes_added"], 0)
        for metric in ("bytes_processed", "bytes_added"):
            with self.subTest(metric=metric):
                self.assertEqual(target["backup_size"]["coverage"][metric], {"reported_jobs": 1, "unavailable_jobs": 0})
                self.assertTrue(report["availability"]["backup_size"]["metrics"][metric]["available"])
                self.assertTrue(report["availability"]["backup_size"]["metrics"][metric]["complete"])

    def test_report_excludes_non_backup_jobs_from_backup_size_aggregation(self):
        repository = InMemoryJobRepository()
        base = datetime(2026, 3, 8, 6, 0, 0)
        repository.save(self._job("b1", base, result_summary={"backup_bytes_processed": 1000, "backup_bytes_added": 400}))
        repository.save(self._job(
            "r1", base + timedelta(minutes=1),
            command="retention.run",
            result_summary={"backup_bytes_processed": 999, "backup_bytes_added": 999},
        ))

        report = self._service(repository).backup_audit_report(
            start_date="2026-03-08",
            end_date="2026-03-08",
            timezone_name="America/New_York",
        )

        matrix = {row["target_id"]: row for row in report["matrix"]}
        target_a = matrix["target-a"]
        # Only the backup.run job contributes; retention.run is excluded.
        self.assertEqual(target_a["backup_size"]["bytes_processed"], 1000)
        self.assertEqual(target_a["backup_size"]["bytes_added"], 400)
        self.assertEqual(target_a["backup_size"]["coverage"]["bytes_processed"]["reported_jobs"], 1)
        self.assertEqual(target_a["backup_size"]["coverage"]["bytes_added"]["reported_jobs"], 1)

    def test_started_job_query_is_half_open_and_consistent_across_repositories(self):
        start = datetime(2026, 3, 8, 5, 0, 0)
        end = datetime(2026, 3, 9, 4, 0, 0)
        expected = ["inside-start", "inside-end"]
        repositories = [InMemoryJobRepository()]
        with tempfile.TemporaryDirectory() as directory:
            repositories.append(SQLiteJobRepository(f"{directory}/control-plane.db"))
            for repository in repositories:
                for job in (
                    self._job("before", start - timedelta(seconds=1)),
                    self._job("inside-start", start),
                    self._job("inside-end", end - timedelta(seconds=1)),
                    self._job("at-end", end),
                    self._job("restore", start, command="restore.run"),
                ):
                    repository.save(job)
                with self.subTest(repository=type(repository).__name__):
                    jobs = repository.list_started_between("backup.run", start, end)
                    self.assertEqual([job.id for job in jobs], expected)

    def test_started_job_query_preserves_result_summary_for_audit_aggregation(self):
        # T5 S2: the date-bounded report query must carry persisted result_summary
        # metrics (not just listing fields) so build_backup_audit_report can
        # aggregate exact Restic byte totals without N+1 reads.
        start = datetime(2026, 3, 8, 5, 0, 0)
        end = datetime(2026, 3, 9, 4, 0, 0)
        repositories = [InMemoryJobRepository()]
        with tempfile.TemporaryDirectory() as directory:
            repositories.append(SQLiteJobRepository(f"{directory}/control-plane.db"))
            for repository in repositories:
                repository.save(self._job(
                    "with-metrics",
                    start + timedelta(minutes=1),
                    result_summary={"backup_bytes_processed": 4096, "backup_bytes_added": 1024},
                ))
                repository.save(self._job(
                    "no-metrics",
                    start + timedelta(minutes=2),
                    result_summary={"progress": {"percent_done": 100.0}},
                ))
                with self.subTest(repository=type(repository).__name__):
                    jobs = repository.list_started_between("backup.run", start, end)
                    by_id = {job.id: job for job in jobs}
                    self.assertEqual(
                        by_id["with-metrics"].result_summary,
                        {"backup_bytes_processed": 4096, "backup_bytes_added": 1024},
                    )
                    self.assertEqual(by_id["no-metrics"].result_summary, {"progress": {"percent_done": 100.0}})
