import tempfile
import unittest
from pathlib import Path

from src.control_plane.domain.models import JobRecord, JobStatus, WorkerScheduleClaimRecord
from src.control_plane.infrastructure.repositories.in_memory import InMemoryJobRepository, InMemoryWorkerScheduleClaimRepository
from src.control_plane.infrastructure.repositories.sqlite import SQLiteJobRepository, SQLiteWorkerScheduleClaimRepository


class WorkerScheduleClaimRepositoryTests(unittest.TestCase):
    def test_handover_blocks_unknown_outcomes_but_active_lookup_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            for repository in (InMemoryJobRepository(), SQLiteJobRepository(str(Path(directory) / "jobs.db"))):
                with self.subTest(repository=type(repository).__name__):
                    job = JobRecord(worker_id="worker-a", command="backup.run", target_id="target-a", status=JobStatus.UNCONFIRMED)
                    repository.save(job)
                    self.assertFalse(repository.has_active_backup_for_target("target-a"))
                    self.assertTrue(repository.has_blocking_backup_for_schedule_handover("target-a"))
                    self.assertFalse(repository.has_blocking_backup_for_schedule_handover("target-b"))
                    job.status = JobStatus.PENDING
                    repository.save(job)
                    self.assertTrue(repository.has_active_backup_for_target("target-a"))

    def test_claim_roundtrip_in_both_repositories(self):
        with tempfile.TemporaryDirectory() as directory:
            for repository in (InMemoryWorkerScheduleClaimRepository(), SQLiteWorkerScheduleClaimRepository(str(Path(directory) / "control.db"))):
                with self.subTest(repository=type(repository).__name__):
                    self.assertIsNone(repository.get_by_worker("worker-a"))
                    claim = WorkerScheduleClaimRecord(worker_id="worker-a", revision="rev-1", target_ids=["target-a"], errors={"target-b": "unavailable"})
                    repository.save(claim)
                    stored = repository.get_by_worker("worker-a")
                    self.assertEqual((stored.revision, stored.target_ids, stored.errors), ("rev-1", ["target-a"], {"target-b": "unavailable"}))
                    self.assertEqual([item.worker_id for item in repository.list()], ["worker-a"])
                    self.assertTrue(repository.delete_by_worker("worker-a"))
                    self.assertFalse(repository.delete_by_worker("worker-a"))
                    self.assertIsNone(repository.get_by_worker("worker-a"))


if __name__ == "__main__":
    unittest.main()
