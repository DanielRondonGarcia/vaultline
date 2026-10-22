import json
import tempfile
import threading
import unittest
from datetime import timedelta
from unittest.mock import patch

from src.control_plane.application.services.control_plane_service import ControlPlaneService
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


class JobOwnershipTests(unittest.TestCase):
    def make_service(self, job_repository=None):
        workers = InMemoryWorkerRepository()
        workers.save(WorkerRecord(name="worker-a", host_name="test", id="worker-a"))
        workers.save(WorkerRecord(name="worker-b", host_name="test", id="worker-b"))
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
        )
        service.heartbeat("worker-a")
        service.heartbeat("worker-b")
        return service

    def test_owner_claim_and_completion(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "worker.self_check")

        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        self.assertEqual(claimed.owner_worker_id, "worker-a")
        self.assertEqual(claimed.attempt_count, 1)
        self.assertTrue(claimed.lease_token)
        completed = service.update_job_status(
            worker_id="worker-a",
            job_id=job.id,
            status=JobStatus.SUCCEEDED,
            lease_token=claimed.lease_token,
        )

        self.assertEqual(completed.status, JobStatus.SUCCEEDED)
        self.assertEqual(service.job_repository.get(job.id).status, JobStatus.SUCCEEDED)

    def test_foreign_worker_cannot_complete(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "worker.self_check")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]

        with self.assertRaisesRegex(ValueError, "does not own"):
            service.update_job_status(
                worker_id="worker-b",
                job_id=job.id,
                status=JobStatus.SUCCEEDED,
                lease_token=claimed.lease_token,
            )
        self.assertEqual(service.job_repository.get(job.id).status, JobStatus.IN_PROGRESS)

    def test_wrong_lease_cannot_complete_but_owner_result_outlives_lease_expiry(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "worker.self_check")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]

        with self.assertRaisesRegex(ValueError, "invalid or stale"):
            service.update_job_status("worker-a", job.id, JobStatus.SUCCEEDED, lease_token="wrong")
        claimed.lease_expires_at = utcnow() - timedelta(seconds=1)
        service.job_repository.save(claimed)
        self.assertEqual(service.job_repository.get(job.id).status, JobStatus.IN_PROGRESS)

        # The lease is a liveness timer, not a reason to discard the owner's result.
        completed = service.update_job_status(
            "worker-a", job.id, JobStatus.SUCCEEDED, lease_token=claimed.lease_token
        )
        self.assertEqual(completed.status, JobStatus.SUCCEEDED)
        self.assertEqual(service.get_job(job.id).status, JobStatus.SUCCEEDED)

    def test_lease_renewal_extends_only_the_current_owner_lease(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "worker.self_check")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        original_expiry = claimed.lease_expires_at

        # Advance the renewal clock explicitly: Windows can return the same
        # timestamp for a claim and an immediate renewal on CPython 3.11.
        renewal_time = original_expiry - timedelta(seconds=service.JOB_LEASE_DURATION_SECONDS - 1)
        with patch("src.control_plane.infrastructure.repositories.in_memory.utcnow", return_value=renewal_time), \
                patch("src.control_plane.application.services.control_plane_service.utcnow", return_value=renewal_time):
            renewed = service.renew_job_lease("worker-a", job.id, claimed.lease_token)

        self.assertEqual(renewed.lease_expires_at, original_expiry + timedelta(seconds=1))
        self.assertIsNotNone(service.worker_repository.get("worker-a").last_seen_at)
        with self.assertRaisesRegex(ValueError, "invalid or stale"):
            service.renew_job_lease("worker-a", job.id, "wrong-token")

        renewed.lease_expires_at = utcnow() - timedelta(seconds=1)
        service.job_repository.save(renewed)
        resumed = service.renew_job_lease("worker-a", job.id, claimed.lease_token)
        self.assertGreater(resumed.lease_expires_at, utcnow())

        with tempfile.TemporaryDirectory() as directory:
            sqlite_service = self.make_service(SQLiteJobRepository(f"{directory}/control-plane.db"))
            sqlite_job = sqlite_service.dispatch_job("worker-a", "worker.self_check")
            sqlite_claimed = sqlite_service.fetch_jobs_for_worker("worker-a")[0]
            sqlite_original_expiry = sqlite_claimed.lease_expires_at
            sqlite_renewal_time = sqlite_original_expiry - timedelta(
                seconds=sqlite_service.JOB_LEASE_DURATION_SECONDS - 1
            )
            with patch("src.control_plane.infrastructure.repositories.sqlite.utcnow", return_value=sqlite_renewal_time), \
                    patch("src.control_plane.application.services.control_plane_service.utcnow", return_value=sqlite_renewal_time):
                sqlite_renewed = sqlite_service.renew_job_lease("worker-a", sqlite_job.id, sqlite_claimed.lease_token)
            self.assertEqual(sqlite_renewed.lease_expires_at, sqlite_original_expiry + timedelta(seconds=1))

    def test_reported_renewal_failure_is_preserved_until_expiry(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "backup.run")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]

        service.record_job_lease_diagnostic(
            worker_id="worker-a",
            job_id=job.id,
            lease_token=claimed.lease_token,
            lease_context={
                "last_renewal": {
                    "outcome": "failed",
                    "error_type": "TimeoutError",
                    "error_category": "connectivity",
                }
            },
        )
        claimed.lease_expires_at = utcnow() - timedelta(seconds=1)
        service.job_repository.save(claimed)

        diagnostics = service.get_job_view(job.id)["diagnostics"]

        self.assertEqual(diagnostics["observed"]["last_renewal"]["outcome"], "failed")
        self.assertEqual(diagnostics["observed"]["last_renewal"]["error_type"], "TimeoutError")
        self.assertEqual(diagnostics["observed"]["last_renewal"]["error_category"], "connectivity")

    def test_read_reconciles_expired_lease_without_worker_fetch(self):
        service = self.make_service()
        interrupted = JobRecord(
            worker_id="worker-a",
            command="restore.run",
            status=JobStatus.IN_PROGRESS,
            owner_worker_id="worker-a",
            lease_token="lease-token",
            lease_issued_at=utcnow() - timedelta(minutes=6),
            lease_expires_at=utcnow() - timedelta(minutes=1),
        )
        service.job_repository.save(interrupted)
        issued_at = interrupted.lease_issued_at.isoformat()
        expires_at = interrupted.lease_expires_at.isoformat()

        observed = service.get_job(interrupted.id)

        self.assertEqual(observed.status, JobStatus.IN_PROGRESS)
        self.assertEqual(observed.result_summary["recovery"], "awaiting_worker_confirmation")
        self.assertEqual(observed.result_summary["diagnostic_code"], "worker_lease_expired")
        self.assertEqual(observed.result_summary["diagnostic_category"], "worker_interrupted")
        self.assertIn("terminal result", observed.result_summary["diagnostic_message"])
        diagnostics = observed.result_summary["lease_diagnostics"]
        self.assertEqual(diagnostics["code"], "worker_lease_expired")
        self.assertEqual(diagnostics["category"], "worker_interrupted")
        self.assertIn("root_cause", diagnostics["unknown"])
        self.assertEqual(diagnostics["observed"]["job_id"], interrupted.id)
        self.assertEqual(diagnostics["observed"]["worker_id"], "worker-a")
        self.assertEqual(diagnostics["observed"]["command"], "restore.run")
        self.assertEqual(diagnostics["observed"]["lease_issued_at"], issued_at)
        self.assertEqual(diagnostics["observed"]["lease_expires_at"], expires_at)
        view = service.get_job_view(interrupted.id)
        self.assertEqual(view["diagnostics"]["code"], "worker_lease_expired")
        self.assertNotIn("lease-token", json.dumps(view, default=str))
        # Ownership evidence survives expiry so a late owner report can still prove itself.
        self.assertEqual(observed.owner_worker_id, "worker-a")
        self.assertEqual(observed.lease_token, "lease-token")
        self.assertIsNone(observed.lease_expires_at)
        self.assertEqual(observed.log_lines.count("Worker lease expired before terminal status was reported."), 1)
        self.assertEqual(service.get_job(interrupted.id).status, JobStatus.IN_PROGRESS)

    def _expired_job(self, service, command="backup.run", lease_token="lease-token"):
        interrupted = JobRecord(
            worker_id="worker-a",
            command=command,
            status=JobStatus.IN_PROGRESS,
            owner_worker_id="worker-a",
            lease_token=lease_token,
            lease_issued_at=utcnow() - timedelta(minutes=6),
            lease_expires_at=utcnow() - timedelta(minutes=1),
        )
        service.job_repository.save(interrupted)
        expired = service.get_job(interrupted.id)
        self.assertEqual(expired.status, JobStatus.IN_PROGRESS)
        self.assertEqual(expired.result_summary["diagnostic_code"], "worker_lease_expired")
        return interrupted

    def test_late_terminal_report_from_owner_replaces_lease_expiry_failure(self):
        for repository in (None, "sqlite"):
            with self.subTest(repository=repository or "memory"), tempfile.TemporaryDirectory() as directory:
                service = self.make_service(
                    SQLiteJobRepository(f"{directory}/control-plane.db") if repository else None
                )
                interrupted = self._expired_job(service)

                with self.assertLogs("src.control_plane.application.services.control_plane_service", level="INFO") as captured:
                    accepted = service.update_job_status(
                        worker_id="worker-a",
                        job_id=interrupted.id,
                        status=JobStatus.SUCCEEDED,
                        result_summary={"status_code": 0, "snapshot_id": "abc123"},
                        log_lines=["Backup finished"],
                        lease_token="lease-token",
                    )
                self.assertTrue(any("Late terminal report accepted after lease expiry" in line for line in captured.output))

                self.assertEqual(accepted.status, JobStatus.SUCCEEDED)
                stored = service.get_job(interrupted.id)
                self.assertEqual(stored.status, JobStatus.SUCCEEDED)
                self.assertEqual(stored.result_summary["snapshot_id"], "abc123")
                self.assertNotIn("diagnostic_code", stored.result_summary)
                self.assertNotIn("recovery", stored.result_summary)
                self.assertIsNotNone(stored.finished_at)
                self.assertIn("Worker lease expired before terminal status was reported.", stored.log_lines)
                self.assertIn("Backup finished", stored.log_lines)
                self.assertTrue(any(line.startswith("Late terminal report accepted after lease expiry") for line in stored.log_lines))

                view = service.get_job_view(interrupted.id)
                self.assertEqual(view["status"], JobStatus.SUCCEEDED)
                self.assertIsNone(view["diagnostics"])
                self.assertEqual(view["result_summary"]["snapshot_id"], "abc123")
                self.assertNotIn("lease-token", json.dumps(view, default=str))

                with self.assertRaisesRegex(ValueError, "not in progress"):
                    service.update_job_status(
                        "worker-a", interrupted.id, JobStatus.FAILED, lease_token="lease-token"
                    )
                self.assertEqual(service.get_job(interrupted.id).status, JobStatus.SUCCEEDED)

    def test_late_terminal_report_without_summary_still_clears_the_synthetic_failure(self):
        service = self.make_service()
        interrupted = self._expired_job(service, command="worker.self_check")

        service.update_job_status("worker-a", interrupted.id, JobStatus.FAILED, lease_token="lease-token")

        stored = service.get_job(interrupted.id)
        self.assertEqual(stored.status, JobStatus.FAILED)
        self.assertNotIn("diagnostic_code", stored.result_summary)
        self.assertIsNone(service.get_job_view(interrupted.id)["diagnostics"])

    def test_late_terminal_report_from_non_owner_or_wrong_token_is_rejected(self):
        service = self.make_service()
        interrupted = self._expired_job(service)

        with self.assertRaisesRegex(ValueError, "does not own"):
            service.update_job_status("worker-b", interrupted.id, JobStatus.SUCCEEDED, lease_token="lease-token")
        with self.assertRaisesRegex(ValueError, "invalid or stale"):
            service.update_job_status("worker-a", interrupted.id, JobStatus.SUCCEEDED, lease_token="guessed")
        with self.assertRaisesRegex(ValueError, "invalid or stale"):
            service.update_job_status("worker-a", interrupted.id, JobStatus.SUCCEEDED, lease_token=None)

        stored = service.get_job(interrupted.id)
        self.assertEqual(stored.status, JobStatus.IN_PROGRESS)
        self.assertEqual(stored.result_summary["diagnostic_code"], "worker_lease_expired")
        self.assertEqual(service.get_job_view(interrupted.id)["diagnostics"]["code"], "worker_lease_expired")

    def test_late_report_cannot_overwrite_a_regular_worker_reported_failure(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "worker.self_check")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        service.update_job_status(
            "worker-a", job.id, JobStatus.FAILED, result_summary={"error": "boom"}, lease_token=claimed.lease_token
        )

        with self.assertRaisesRegex(ValueError, "not in progress"):
            service.update_job_status("worker-a", job.id, JobStatus.SUCCEEDED, lease_token=claimed.lease_token)
        self.assertEqual(service.get_job(job.id).status, JobStatus.FAILED)
        self.assertEqual(service.get_job(job.id).result_summary["error"], "boom")

    def test_duplicate_and_canceled_completion_are_rejected(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "worker.self_check")
        claimed = service.fetch_jobs_for_worker("worker-a")[0]
        service.update_job_status("worker-a", job.id, JobStatus.SUCCEEDED, lease_token=claimed.lease_token)
        acknowledged = service.update_job_status("worker-a", job.id, JobStatus.SUCCEEDED, lease_token=claimed.lease_token)
        self.assertEqual(acknowledged.status, JobStatus.SUCCEEDED)

        canceled = service.dispatch_job("worker-a", "worker.self_check")
        canceled_claim = service.fetch_jobs_for_worker("worker-a")[0]
        service.cancel_job(canceled.id)
        with self.assertRaisesRegex(ValueError, "canceled"):
            service.update_job_status(
                "worker-a", canceled.id, JobStatus.SUCCEEDED, lease_token=canceled_claim.lease_token
            )
        self.assertEqual(service.job_repository.get(canceled.id).status, JobStatus.CANCELED)

    def test_cancel_fills_empty_terminal_summary_and_logs(self):
        service = self.make_service()
        job = service.dispatch_job("worker-a", "restore.run")
        service.fetch_jobs_for_worker("worker-a")

        canceled = service.cancel_job(job.id)

        self.assertEqual(canceled.status, JobStatus.CANCELED)
        self.assertEqual(canceled.result_summary["recovery"], "operator_canceled")
        self.assertEqual(
            canceled.log_lines,
            ["Job canceled by operator before terminal worker completion."],
        )

    def test_fetch_jobs_leases_only_the_oldest_pending_job_until_next_poll(self):
        def assert_one_at_a_time(job_repository):
            service = self.make_service(job_repository)
            first_job = service.dispatch_job("worker-a", "backup.run")
            second_job = service.dispatch_job("worker-a", "backup.run")

            first_claim = service.fetch_jobs_for_worker("worker-a")

            self.assertEqual([job.id for job in first_claim], [first_job.id])
            waiting_job = service.get_job(second_job.id)
            self.assertEqual(waiting_job.status, JobStatus.PENDING)
            self.assertIsNone(waiting_job.lease_token)
            self.assertIsNone(waiting_job.lease_expires_at)
            self.assertEqual(waiting_job.attempt_count, 0)

            service.update_job_status(
                worker_id="worker-a",
                job_id=first_job.id,
                status=JobStatus.SUCCEEDED,
                lease_token=first_claim[0].lease_token,
            )
            second_claim = service.fetch_jobs_for_worker("worker-a")

            self.assertEqual([job.id for job in second_claim], [second_job.id])
            self.assertEqual(second_claim[0].status, JobStatus.IN_PROGRESS)
            self.assertTrue(second_claim[0].lease_token)
            self.assertEqual(second_claim[0].attempt_count, 1)

        assert_one_at_a_time(InMemoryJobRepository())
        with tempfile.TemporaryDirectory() as directory:
            assert_one_at_a_time(SQLiteJobRepository(f"{directory}/control-plane.db"))

    def test_interactive_claim_prefers_explorer_job_without_leasing_backup(self):
        def assert_priority(job_repository):
            service = self.make_service(job_repository)
            backup = service.dispatch_job("worker-a", "backup.run")
            explorer = service.dispatch_job("worker-a", "snapshot.search")

            claimed = service.fetch_interactive_jobs_for_worker("worker-a")

            self.assertEqual([job.id for job in claimed], [explorer.id])
            queued_backup = service.get_job(backup.id)
            self.assertEqual(queued_backup.status, JobStatus.PENDING)
            self.assertIsNone(queued_backup.lease_token)
            self.assertIsNone(queued_backup.lease_expires_at)

        assert_priority(InMemoryJobRepository())
        with tempfile.TemporaryDirectory() as directory:
            assert_priority(SQLiteJobRepository(f"{directory}/control-plane.db"))

    def test_in_memory_claim_does_not_double_assign(self):
        repository = InMemoryJobRepository()
        repository.save(JobRecord(worker_id="worker-a", command="worker.self_check"))
        results = []

        def claim():
            results.append(repository.claim_pending_for_worker("worker-a"))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(len(items) for items in results), 1)

    def test_sqlite_claim_does_not_double_assign(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/control-plane.db"
            first = SQLiteJobRepository(path)
            second = SQLiteJobRepository(path)
            first.save(JobRecord(worker_id="worker-a", command="worker.self_check"))
            results = []

            def claim(repository):
                results.append(repository.claim_pending_for_worker("worker-a"))

            threads = [threading.Thread(target=claim, args=(repository,)) for repository in (first, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sum(len(items) for items in results), 1)


if __name__ == "__main__":
    unittest.main()
