import copy
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import Mock

import test_job_ownership
from src.control_plane.domain.models import JobStatus, utcnow
from src.control_plane.infrastructure.repositories.sqlite import SQLiteJobRepository


class ControlPlaneAsyncRecoveryTests(unittest.TestCase):
    def services(self, directory):
        for repository in (None, SQLiteJobRepository(f"{directory}/cp.db")):
            yield test_job_ownership.JobOwnershipTests().make_service(repository)

    def test_expiry_is_unconfirmed_not_failed_and_owner_can_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            for service in self.services(directory):
                with self.subTest(repository=type(service.job_repository).__name__):
                    service._schedule_failed_backup_notifications = Mock()
                    job = service.dispatch_job("worker-a", "backup.run")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    claimed.lease_expires_at = utcnow() - timedelta(seconds=1)
                    service.job_repository.save(claimed)
                    view = service.get_job_view(job.id)
                    self.assertEqual(view["status"], JobStatus.IN_PROGRESS)
                    self.assertIsNone(view["finished_at"])
                    self.assertEqual(view["diagnostics"]["code"], "worker_lease_expired")
                    self.assertNotIn("error", view["result_summary"])
                    service._schedule_failed_backup_notifications.assert_not_called()
                    self.assertEqual(service.fetch_jobs_for_worker("worker-a"), [])
                    before = copy.deepcopy(service.get_job_view(job.id))
                    with self.assertRaisesRegex(ValueError, "invalid or stale"):
                        service.update_job_progress("worker-a", job.id, 1, {}, ["bad"], lease_token="wrong")
                    self.assertEqual(service.get_job_view(job.id), before)
                    service.update_job_progress("worker-a", job.id, 1, {"phase": "backup"}, ["replayed"], lease_token=claimed.lease_token)
                    service.update_job_progress("worker-a", job.id, 1, {}, ["duplicate"], lease_token=claimed.lease_token)
                    self.assertEqual(service.get_job(job.id).log_lines.count("replayed"), 1)
                    self.assertNotIn("duplicate", service.get_job(job.id).log_lines)
                    renewed = service.renew_job_lease("worker-a", job.id, claimed.lease_token)
                    self.assertGreater(renewed.lease_expires_at, utcnow())
                    self.assertIsNone(service.get_job_view(job.id)["diagnostics"])
                    service.update_job_status("worker-a", job.id, JobStatus.SUCCEEDED, {"status_code": 0}, ["done"], claimed.lease_token)
                    self.assertEqual(service.get_job_view(job.id)["status"], JobStatus.SUCCEEDED)
                    service._schedule_failed_backup_notifications.assert_not_called()

    def test_lost_terminal_ack_is_idempotent_and_conflicting_result_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for service in self.services(directory):
                with self.subTest(repository=type(service.job_repository).__name__):
                    job = service.dispatch_job("worker-a", "backup.run")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    service._schedule_failed_backup_notifications = Mock()
                    args = ("worker-a", job.id, JobStatus.FAILED, {"status_code": 1, "error": "ValueError: invalid configuration"}, ["ValueError: invalid configuration"], claimed.lease_token)
                    service.update_job_status(*args)
                    before = copy.deepcopy(service.get_job_view(job.id))
                    service.update_job_status(*args)
                    self.assertEqual(service.get_job_view(job.id), before)
                    service._schedule_failed_backup_notifications.assert_called_once()
                    with self.assertRaisesRegex(ValueError, "not in progress"):
                        service.update_job_status("worker-a", job.id, JobStatus.SUCCEEDED, lease_token=claimed.lease_token)
                    self.assertEqual(service.get_job_view(job.id), before)


if __name__ == "__main__":
    unittest.main()
