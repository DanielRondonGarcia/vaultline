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
    def _job(job_id, started_at, *, command="backup.run", status=JobStatus.SUCCEEDED, target_id="target-a", worker_id="worker-a"):
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
