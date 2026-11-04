import tempfile
import unittest

from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient
from src.worker_agent.infrastructure.security.progress_report_journal import (
    WorkerProgressReportJournal,
)
from src.worker_agent.infrastructure.security.terminal_report_journal import (
    WorkerTerminalReportJournal,
)

STAMP = "2024-05-05T10:00:00.123456789Z"


class WorkerTerminalReportJournalTimestampTests(unittest.TestCase):
    @staticmethod
    def record(**overrides):
        kwargs = dict(
            job_id="job-a",
            worker_id="worker-a",
            lease_token="lease",
            status="progress",
            result_summary={"sequence": 1, "progress": {}},
            log_lines=["one", "two"],
        )
        kwargs.update(overrides)
        return WorkerTerminalReportJournal.bounded_record(**kwargs)

    def test_aligned_timestamps_are_stored_as_optional_parallel_metadata(self):
        record = self.record(log_timestamps=[STAMP, None])

        self.assertEqual(record["log_lines"], ["one", "two"])
        self.assertEqual(record["log_timestamps"], [STAMP, None])

    def test_misaligned_or_malformed_timestamps_are_dropped_not_stored(self):
        self.assertNotIn("log_timestamps", self.record(log_timestamps=[STAMP]))
        self.assertNotIn("log_timestamps", self.record(log_timestamps=STAMP))
        self.assertNotIn("log_timestamps", self.record(log_timestamps={"line": STAMP}))
        self.assertNotIn(
            "log_timestamps",
            self.record(log_lines=["only"], log_timestamps=[STAMP, None]),
        )

    def test_over_trimmed_records_keep_lines_and_timestamps_aligned(self):
        long_line = "x" * 2_500_000
        stamps = ["2024-05-05T10:00:00Z", STAMP, "2024-05-05T10:00:02Z"]
        record = self.record(
            log_lines=[long_line, "mid", "end"],
            log_timestamps=list(stamps),
        )

        self.assertEqual(record["log_lines"][-1], "end")
        self.assertLess(len(record["log_lines"]), 3)
        self.assertEqual(len(record["log_timestamps"]), len(record["log_lines"]))
        offset = 3 - len(record["log_lines"])
        self.assertEqual(record["log_timestamps"], stamps[offset:])

    def test_progress_journal_round_trips_timestamps_through_the_spool(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = WorkerProgressReportJournal(f"{directory}/spool.json")
            journal.write(
                [
                    WorkerProgressReportJournal.batch(
                        "worker-a", "job-a", "lease", 1, {"phase": "backup"},
                        ["line"], log_timestamps=[STAMP],
                    )
                ]
            )

            records = WorkerProgressReportJournal(f"{directory}/spool.json").load()

            self.assertEqual(records[0]["log_lines"], ["line"])
            self.assertEqual(records[0]["log_timestamps"], [STAMP])


class ControlPlaneClientTimestampPayloadTests(unittest.TestCase):
    @staticmethod
    def capturing_client(sent):
        client = ControlPlaneClient("http://control-plane.example.invalid")

        def capture(path, payload, authenticate=True, extra_headers=None):
            sent.append((path, payload))
            return {}

        client._post = capture
        return client

    def test_optional_progress_timestamps_are_sent_only_when_present(self):
        sent = []
        client = self.capturing_client(sent)

        client.update_job_progress(
            worker_id="worker-a", job_id="job-a", sequence=1,
            progress={"phase": "backup"}, log_lines=["one", "two"],
            lease_token="lease", log_timestamps=[STAMP, None],
        )
        self.assertEqual(sent[-1][1]["log_timestamps"], [STAMP, None])

        client.update_job_progress(
            worker_id="worker-a", job_id="job-a", sequence=2,
            progress={"phase": "backup"}, log_lines=["three"], lease_token="lease",
        )
        self.assertNotIn("log_timestamps", sent[-1][1])

    def test_optional_status_timestamps_are_sent_only_when_present(self):
        sent = []
        client = self.capturing_client(sent)

        client.update_job_status(
            worker_id="worker-a", job_id="job-a", status="succeeded",
            result_summary={}, log_lines=["done"], lease_token="lease",
            log_timestamps=[None],
        )
        self.assertEqual(sent[-1][1]["log_timestamps"], [None])

        client.update_job_status(
            worker_id="worker-a", job_id="job-a", status="succeeded",
            result_summary={}, log_lines=["done"], lease_token="lease",
        )
        self.assertNotIn("log_timestamps", sent[-1][1])


if __name__ == "__main__":
    unittest.main()