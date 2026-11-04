"""S8 resource guards around the 24-hour unconfirmed outcome feature.

The repository must never load the whole jobs table to answer the
reconciliation question, and the reconciliation itself must be answerable
from a bounded candidate query for both in-memory and SQLite repositories.
"""

import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

import test_job_ownership
from src.control_plane.domain.models import JobRecord, JobStatus, utcnow
from src.control_plane.infrastructure.repositories.in_memory import InMemoryJobRepository
from src.control_plane.infrastructure.repositories.sqlite import SQLiteJobRepository


LEASE_DIAGNOSTIC_CODE = "worker_lease_expired"


class UnconfirmedCandidateQueryTests(unittest.TestCase):
    def repositories(self, directory):
        yield InMemoryJobRepository()
        yield SQLiteJobRepository(f"{directory}/cp.db")

    def test_filtered_query_returns_only_awaiting_confirmation_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            for repository, repository_name in (
                (InMemoryJobRepository(), "InMemoryJobRepository"),
                (SQLiteJobRepository(f"{directory}/cp.db"), "SQLiteJobRepository"),
            ):
                with self.subTest(repository=repository_name):
                    repository.save(
                        JobRecord(
                            worker_id="worker-a",
                            command="backup.run",
                            id="cand",
                            status=JobStatus.IN_PROGRESS,
                            result_summary={
                                "diagnostic_code": LEASE_DIAGNOSTIC_CODE,
                                "recovery": "awaiting_worker_confirmation",
                            },
                        )
                    )
                    # A running job without the lease-expired diagnostic is NOT
                    # a candidate, and neither is a finished job.
                    repository.save(
                        JobRecord(
                            worker_id="worker-a",
                            command="snapshots.list",
                            id="healthy",
                            status=JobStatus.IN_PROGRESS,
                        )
                    )
                    repository.save(
                        JobRecord(
                            worker_id="worker-a",
                            command="backup.run",
                            id="done",
                            status=JobStatus.SUCCEEDED,
                            result_summary={"diagnostic_code": LEASE_DIAGNOSTIC_CODE},
                        )
                    )

                    candidates = [job.id for job in repository.list_for_unconfirmed_outcomes()]

                    self.assertEqual(candidates, ["cand"])

    def test_reconciliation_never_loads_the_whole_jobs_table(self):
        with tempfile.TemporaryDirectory() as directory:
            for repository in (None, SQLiteJobRepository(f"{directory}/cp.db")):
                with self.subTest(repository=type(repository).__name__ if repository else "InMemoryJobRepository"):
                    service = test_job_ownership.JobOwnershipTests().make_service(repository)
                    job = service.dispatch_job("worker-a", "backup.run")
                    claimed = service.fetch_jobs_for_worker("worker-a")[0]
                    claimed.lease_expires_at = utcnow() - timedelta(hours=24, minutes=2)
                    service.job_repository.save(claimed)

                    forbidden = AssertionError("job_repository.list() must not be called during reconciliation")
                    with patch.object(service.job_repository, "list", side_effect=forbidden):
                        service._reconcile_expired_jobs()
                        view = service.get_job(job.id)

                    self.assertEqual(view.status, JobStatus.UNCONFIRMED)


if __name__ == "__main__":
    unittest.main()