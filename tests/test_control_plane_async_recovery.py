import copy
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import Mock

import test_job_ownership
from src.control_plane.domain.models import BackupTargetRecord, JobStatus, utcnow
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


class OutcomeUnknownDeadlineTests(unittest.TestCase):
    """S7: after at least 24 hours without worker confirmation the outcome is unknown.

    Missing communication never becomes a synthetic failure or success, the
    job stops blocking new tasks, and the authenticated original owner can
    still deliver a late terminal report.
    """

    def services_with_target(self, directory):
        for service in ControlPlaneAsyncRecoveryTests().services(directory):
            target = BackupTargetRecord(name="target", worker_id="worker-a")
            service.target_repository.save(target)
            yield service, target

    @staticmethod
    def _view(service, job_id):
        service._reconcile_expired_jobs()
        return service.get_job_view(job_id)

    def test_before_24h_lease_expiry_keeps_awaiting_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            for service, target in self.services_with_target(directory):
                with self.subTest(repository=type(service.job_repository).__name__):
                    service._schedule_failed_backup_notifications = Mock()
                    job = service.dispatch_job("worker-a", "backup.run", target_id=target.id, trigger="schedule")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    claimed.lease_expires_at = utcnow() - timedelta(hours=24, minutes=-2)
                    expired_by = utcnow() - claimed.lease_expires_at
                    self.assertGreaterEqual(expired_by, timedelta(hours=23, minutes=58))
                    service.job_repository.save(claimed)

                    view = self._view(service, job.id)

                    self.assertEqual(view["status"], JobStatus.IN_PROGRESS)
                    self.assertIsNone(view["finished_at"])
                    self.assertEqual(view["result_summary"]["recovery"], "awaiting_worker_confirmation")
                    self.assertEqual(view["result_summary"]["diagnostic_code"], "worker_lease_expired")
                    self.assertTrue(service.has_active_backup_for_target(target.id))
                    service._schedule_failed_backup_notifications.assert_not_called()

    def test_at_24h_without_confirmation_outcome_is_unknown_and_new_tasks_unlock(self):
        with tempfile.TemporaryDirectory() as directory:
            for service, target in self.services_with_target(directory):
                with self.subTest(repository=type(service.job_repository).__name__):
                    service._schedule_failed_backup_notifications = Mock()
                    job = service.dispatch_job("worker-a", "backup.run", target_id=target.id, trigger="schedule")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    claimed.lease_expires_at = utcnow() - timedelta(hours=24, minutes=2)
                    service.job_repository.save(claimed)

                    view = self._view(service, job.id)

                    self.assertEqual(view["status"], JobStatus.UNCONFIRMED)
                    self.assertNotIn(view["status"], (JobStatus.FAILED, JobStatus.SUCCEEDED))
                    self.assertIsNone(view["finished_at"])
                    summary = view["result_summary"]
                    self.assertEqual(summary["recovery"], "outcome_unknown")
                    self.assertEqual(summary["diagnostic_code"], "worker_lease_expired")
                    self.assertNotIn("error", summary)
                    service._schedule_failed_backup_notifications.assert_not_called()

                    # New tasks are allowed: scheduled backups and snapshot sync unblock.
                    self.assertFalse(service.has_active_backup_for_target(target.id))

                    before = copy.deepcopy(self._view(service, job.id))
                    # Ownership and token fencing survive the unknown boundary.
                    with self.assertRaisesRegex(ValueError, "does not own job"):
                        service.update_job_status("worker-b", job.id, JobStatus.FAILED, {"status_code": 1}, [], claimed.lease_token)
                    with self.assertRaisesRegex(ValueError, "invalid or stale"):
                        service.update_job_status("worker-a", job.id, JobStatus.FAILED, {"status_code": 1}, [], "wrong-token")
                    with self.assertRaisesRegex(ValueError, "not in progress"):
                        service.renew_job_lease("worker-a", job.id, claimed.lease_token)
                    progress = service.update_job_progress("worker-a", job.id, 99, {"phase": "backup"}, ["late batch"], lease_token=claimed.lease_token)
                    self.assertEqual(progress.status, JobStatus.UNCONFIRMED)
                    self.assertNotIn("late batch", service.get_job(job.id).log_lines)
                    self.assertEqual(self._view(service, job.id), before)

                    # A valid late terminal report from the owner is still accepted.
                    service.update_job_status("worker-a", job.id, JobStatus.SUCCEEDED, {"status_code": 0}, ["real result"], claimed.lease_token)
                    view = self._view(service, job.id)
                    self.assertEqual(view["status"], JobStatus.SUCCEEDED)
                    self.assertIsNotNone(view["finished_at"])
                    service._schedule_failed_backup_notifications.assert_not_called()

    def test_second_reconciliation_of_an_unknown_outcome_job_is_a_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            for service, target in self.services_with_target(directory):
                with self.subTest(repository=type(service.job_repository).__name__):
                    service._schedule_failed_backup_notifications = Mock()
                    job = service.dispatch_job("worker-a", "backup.run", target_id=target.id, trigger="schedule")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    claimed.lease_expires_at = utcnow() - timedelta(hours=24, minutes=2)
                    service.job_repository.save(claimed)
                    self._view(service, job.id)  # first reconciliation flips to unknown
                    flipped = service.job_repository.get(job.id)
                    self.assertEqual(flipped.status, JobStatus.UNCONFIRMED)

                    before = copy.deepcopy(service.job_repository.get(job.id))
                    reconciled_again = service._reconcile_expired_jobs()
                    reconciled_third = service._reconcile_expired_jobs()

                    # Parity with SQLite: a job already recorded as unknown is
                    # not reprocessed; repeated reconciliations report 0 and
                    # touch nothing (status, updated_at, result_summary, logs,
                    # finished_at, owner and lease token stay exactly equal).
                    self.assertEqual(reconciled_again, 0)
                    self.assertEqual(reconciled_third, 0)
                    self.assertEqual(service.job_repository.get(job.id), before)
                    view = self._view(service, job.id)
                    self.assertEqual(view["status"], JobStatus.UNCONFIRMED)
                    self.assertIsNone(view["finished_at"])
                    self.assertEqual(view["result_summary"]["recovery"], "outcome_unknown")
                    service._schedule_failed_backup_notifications.assert_not_called()

    def test_late_failed_terminal_report_after_unknown_outcome_notifies_once(self):
        with tempfile.TemporaryDirectory() as directory:
            for service, target in self.services_with_target(directory):
                with self.subTest(repository=type(service.job_repository).__name__):
                    service._schedule_failed_backup_notifications = Mock()
                    job = service.dispatch_job("worker-a", "backup.run", target_id=target.id, trigger="schedule")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    claimed.lease_expires_at = utcnow() - timedelta(hours=24, minutes=2)
                    service.job_repository.save(claimed)
                    self._view(service, job.id)  # flips to unknown

                    args = (
                        "worker-a",
                        job.id,
                        JobStatus.FAILED,
                        {"status_code": 1, "error": "ValueError: invalid configuration"},
                        ["ValueError: invalid configuration"],
                        claimed.lease_token,
                    )
                    service.update_job_status(*args)
                    view = self._view(service, job.id)
                    self.assertEqual(view["status"], JobStatus.FAILED)
                    service._schedule_failed_backup_notifications.assert_called_once()

                    # An exact resend of the lost-ACK report stays idempotent.
                    before = copy.deepcopy(self._view(service, job.id))
                    service.update_job_status(*args)
                    self.assertEqual(self._view(service, job.id), before)
                    service._schedule_failed_backup_notifications.assert_called_once()

    def test_unconfirmed_snapshot_sync_allows_a_new_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            for service, target in self.services_with_target(directory):
                with self.subTest(repository=type(service.job_repository).__name__):
                    job = service.dispatch_job("worker-a", "snapshots.list", target_id=target.id, trigger="automatic")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    claimed.lease_expires_at = utcnow() - timedelta(hours=24, minutes=2)
                    service.job_repository.save(claimed)
                    self._view(service, job.id)

                    self.assertEqual(self._view(service, job.id)["status"], JobStatus.UNCONFIRMED)
                    self.assertFalse(service.has_active_snapshot_sync_for_target(target.id))
                    new_sync = service.dispatch_snapshot_sync_for_target(target.id)
                    self.assertNotEqual(new_sync.id, job.id)
                    self.assertEqual(new_sync.status, JobStatus.PENDING)

    def test_operator_can_cancel_an_unknown_outcome_job(self):
        with tempfile.TemporaryDirectory() as directory:
            for service, target in self.services_with_target(directory):
                with self.subTest(repository=type(service.job_repository).__name__):
                    job = service.dispatch_job("worker-a", "backup.run", target_id=target.id, trigger="schedule")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    claimed.lease_expires_at = utcnow() - timedelta(hours=24, minutes=2)
                    service.job_repository.save(claimed)
                    self._view(service, job.id)

                    canceled = service.cancel_job(job.id)

                    self.assertEqual(canceled.status, JobStatus.CANCELED)
                    self.assertIsNotNone(canceled.finished_at)
                    # Operator cancellation stays definitive; a late report cannot reopen it.
                    with self.assertRaisesRegex(ValueError, "canceled"):
                        service.update_job_status("worker-a", job.id, JobStatus.SUCCEEDED, {"status_code": 0}, [], claimed.lease_token)

    def test_owner_progress_refreshes_the_unknown_deadline_and_keeps_dispatch_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            for service, target in self.services_with_target(directory):
                with self.subTest(repository=type(service.job_repository).__name__):
                    from datetime import datetime

                    service._schedule_failed_backup_notifications = Mock()
                    job = service.dispatch_job("worker-a", "backup.run", target_id=target.id, trigger="schedule")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    claimed.lease_expires_at = utcnow() - timedelta(hours=23)
                    service.job_repository.save(claimed)

                    view = self._view(service, job.id)
                    self.assertEqual(view["status"], JobStatus.IN_PROGRESS)
                    deadline_before = datetime.fromisoformat(
                        view["result_summary"]["confirmation_deadline_at"].replace("Z", "+00:00")
                    if view["result_summary"]["confirmation_deadline_at"].endswith("Z")
                    else view["result_summary"]["confirmation_deadline_at"])

                    service.update_job_progress(
                        "worker-a", job.id, 1, {"phase": "backup"}, ["still running"],
                        lease_token=claimed.lease_token,
                    )

                    view = self._view(service, job.id)
                    self.assertEqual(
                        view["result_summary"]["diagnostic_code"], "worker_lease_expired"
                    )
                    deadline_after = datetime.fromisoformat(
                        view["result_summary"]["confirmation_deadline_at"].replace("Z", "+00:00")
                    if view["result_summary"]["confirmation_deadline_at"].endswith("Z")
                    else view["result_summary"]["confirmation_deadline_at"])
                    self.assertGreater(deadline_after, deadline_before)

                    # The execution can be known to still run: no unknown flip, no new tasks.
                    self.assertEqual(view["status"], JobStatus.IN_PROGRESS)
                    self.assertTrue(service.has_active_backup_for_target(target.id))
                    service._schedule_failed_backup_notifications.assert_not_called()
                    self.assertIn("still running", service.get_job(job.id).log_lines)


if __name__ == "__main__":
    unittest.main()
