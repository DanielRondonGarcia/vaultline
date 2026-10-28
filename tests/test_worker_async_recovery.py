import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.worker_agent.application.services.worker_agent_service import WorkerAgentService, _JobProgressReporter
from src.worker_agent.domain.models import WorkerAgentConfig


class WorkerAsyncRecoveryTests(unittest.TestCase):
    def test_failed_progress_survives_restart_and_replays_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            job = {"id": "job-a", "lease_token": "lease-secret"}
            first_client = Mock()
            # Reporter deliberately requires a concrete method, not a dynamic Mock attribute.
            class Client:
                def __init__(self, offline):
                    self.offline = offline
                    self.calls = []
                def update_job_progress(self, **kwargs):
                    self.calls.append(kwargs)
                    if self.offline:
                        raise ConnectionError("offline")
            first_client = Client(True)
            reporter = _JobProgressReporter(first_client, "worker-a", job, spool_directory=directory)
            reporter.start()
            reporter.emit("first line\nsecond line\n", {"phase": "backup"}, force=True)
            reporter.finish()
            self.assertTrue(list(Path(directory).glob("*.json")))

            online = Client(False)
            restarted = _JobProgressReporter(online, "worker-a", job, spool_directory=directory)
            restarted.replay_pending()
            self.assertEqual([line for call in online.calls for line in call["log_lines"]], ["first line", "second line"])
            self.assertEqual([call["sequence"] for call in online.calls], sorted(set(call["sequence"] for call in online.calls)))
            self.assertEqual(online.calls[-1]["progress"], {"phase": "backup"})
            self.assertEqual(list(Path(directory).glob("*.json")), [])

    def test_active_sender_retries_without_reexecuting_runtime(self):
        delivered = threading.Event()
        class Client:
            calls = 0
            def update_job_progress(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("offline")
                if "buffered line" in kwargs["log_lines"]:
                    delivered.set()
        client = Client()
        reporter = _JobProgressReporter(client, "worker-a", {"id": "job-a", "lease_token": "lease"})
        reporter.RETRY_MIN_SECONDS = 0.01
        reporter.emit("buffered line\n", force=True)
        try:
            self.assertTrue(delivered.wait(2))
        finally:
            reporter.finish()

    def test_spooled_logs_redact_job_secrets_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            class Client:
                def update_job_progress(self, **kwargs):
                    raise ConnectionError("offline")
            job = {"id": "job-a", "lease_token": "lease-secret", "payload": {"environment": {"RESTIC_PASSWORD": "password-secret"}}}
            reporter = _JobProgressReporter(Client(), "worker-a", job, spool_directory=directory)
            reporter.emit("password-secret lease-secret\n", force=True)
            reporter.finish()
            content = next(Path(directory).glob("*.json")).read_text()
            self.assertNotIn("password-secret", content)
            # The lease proof is stored privately, but must never appear in the log payload.
            online = Mock()
            class Receiver:
                def update_job_progress(self, **kwargs):
                    online(**kwargs)
            replay = _JobProgressReporter(Receiver(), "worker-a", job, spool_directory=directory)
            replay.replay_pending()
            logs = online.call_args.kwargs["log_lines"]
            self.assertNotIn("lease-secret", "\n".join(logs))
            self.assertNotIn("password-secret", "\n".join(logs))
            if os.name != "nt":
                self.assertEqual(os.stat(directory).st_mode & 0o077, 0)

    def test_terminal_result_is_durable_before_first_network_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            class Client:
                def update_job_status(self, **kwargs):
                    self.request = kwargs
                    self.saved = service.terminal_report_journal.load()
                    return {"status": kwargs["status"]}
            client = Client()
            runtime = Mock()
            stamp = "2024-05-05T10:00:00Z"
            runtime.run_runtime_job.return_value = {
                "success": True, "status_code": 0, "logs": "done\n", "stderr": "",
                "log_timestamps": [stamp],
            }
            service = WorkerAgentService(
                WorkerAgentConfig("http://cp", "worker", "host", worker_id="worker-a"),
                client, runtime, recovery_file=str(Path(directory) / "recovery.json"),
            )
            service._process_jobs("worker-a", [{"id": "job-a", "command": "backup.run", "lease_token": "lease", "payload": {"environment": {}}}])
            self.assertEqual(len(client.saved), 1)
            self.assertEqual(client.saved[0]["status"], "succeeded")
            self.assertEqual(client.saved[0]["log_timestamps"], [stamp])
            self.assertEqual(client.request["log_timestamps"], [stamp])
            self.assertEqual(service.terminal_report_journal.load(), [])


    def test_docker_source_timestamps_survive_progress_spool_and_replay_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            job = {"id": "job-a", "lease_token": "lease-secret"}

            class Client:
                def __init__(self, offline):
                    self.offline = offline
                    self.calls = []

                def update_job_progress(self, **kwargs):
                    self.calls.append(kwargs)
                    if self.offline:
                        raise ConnectionError("offline")

            stamp = "2024-05-05T10:00:00.123456789Z"
            first = _JobProgressReporter(Client(True), "worker-a", job, spool_directory=directory)
            first.start()
            first.emit(
                "first line\nsecond line\n",
                {"phase": "backup"},
                force=True,
                timestamps=[stamp, None],
            )
            first.finish()

            records = json.loads(next(Path(directory).glob("*.json")).read_text())
            record = next(item for item in records if item["log_lines"])
            self.assertEqual(record["job_id"], "job-a")
            self.assertEqual(record["log_timestamps"], [stamp, None])
            self.assertEqual(record["log_lines"], ["first line", "second line"])

            online = Client(False)
            restarted = _JobProgressReporter(online, "worker-a", job, spool_directory=directory)
            restarted.replay_pending()
            call = next(call for call in reversed(online.calls) if call["log_lines"])
            self.assertEqual(call["log_lines"], ["first line", "second line"])
            self.assertEqual(call["log_timestamps"], [stamp, None])
            self.assertEqual(list(Path(directory).glob("*.json")), [])

    def test_timestamp_unsupported_sender_preserves_lease_context(self):
        stamp = "2024-05-05T10:00:00Z"
        class LegacyClient:
            def update_job_progress(
                self, worker_id, job_id, sequence, progress, log_lines,
                lease_token=None, lease_context=None,
            ):
                self.call = {
                    "worker_id": worker_id,
                    "job_id": job_id,
                    "sequence": sequence,
                    "progress": progress,
                    "log_lines": log_lines,
                    "lease_token": lease_token,
                    "lease_context": lease_context,
                }
        client = LegacyClient()
        diagnostics = Mock()
        diagnostics.snapshot.return_value = {"owner": "worker-a"}
        reporter = _JobProgressReporter(
            client, "worker-a", {"id": "job-a", "lease_token": "lease"},
            lease_diagnostics=diagnostics,
        )

        self.assertTrue(reporter._send(1, {"phase": "backup"}, ["line"], [stamp]))
        self.assertEqual(client.call["lease_context"], {"owner": "worker-a"})
        self.assertEqual(client.call["log_lines"], ["line"])

    def test_partial_line_timestamp_survives_callback_chunk_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            class Client:
                def update_job_progress(self, **kwargs):
                    raise ConnectionError("offline")

            reporter = _JobProgressReporter(
                Client(), "worker-a", {"id": "job-a", "lease_token": "lease"},
                spool_directory=directory,
            )
            stamp = "2024-05-05T10:00:01Z"
            reporter.emit("partial ", timestamps=[stamp])
            reporter.emit("continuation\n", force=True, timestamps=[None])
            reporter.finish()

            records = json.loads(next(Path(directory).glob("*.json")).read_text())
            record = next(item for item in records if item["log_lines"])
            self.assertEqual(record["log_lines"], ["partial continuation"])
            self.assertEqual(record["log_timestamps"], [stamp])

    def test_plain_progress_emits_carry_no_timestamp_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            job = {"id": "job-a", "lease_token": "lease-secret"}

            class Client:
                def __init__(self):
                    self.calls = []

                def update_job_progress(self, **kwargs):
                    self.calls.append(kwargs)
                    raise ConnectionError("offline")

            first = _JobProgressReporter(Client(), "worker-a", job, spool_directory=directory)
            first.emit("first line\nsecond line\n", {"phase": "backup"}, force=True)
            first.finish()

            record = json.loads(next(Path(directory).glob("*.json")).read_text())[0]
            self.assertNotIn("log_timestamps", record)
            self.assertEqual(record["log_lines"], ["first line", "second line"])

    def test_retention_trims_lines_and_timestamps_together(self):
        with tempfile.TemporaryDirectory() as directory:
            class Client:
                def update_job_progress(self, **kwargs):
                    raise ConnectionError("offline")

            job = {"id": "job-a", "lease_token": "lease"}
            reporter = _JobProgressReporter(Client(), "worker-a", job, spool_directory=directory)
            stamps = [f"2024-05-05T10:{index:02d}Z" for index in range(40)]
            text = "".join(f"line-{index}\n" for index in range(40))

            reporter.emit(text, force=True, timestamps=stamps)
            reporter.finish()

            record = json.loads(next(Path(directory).glob("*.json")).read_text())[0]
            self.assertEqual(len(record["log_lines"]), _JobProgressReporter.MAX_PENDING_LINES)
            self.assertEqual(record["log_lines"][0], "line-8")
            self.assertEqual(record["log_timestamps"][0], stamps[8])
            self.assertEqual(record["log_timestamps"][-1], stamps[-1])

    def test_timestamp_delivery_falls_back_when_the_client_cannot_accept_them(self):
        class Client:
            def __init__(self):
                self.calls = []

            def update_job_progress(
                self, worker_id, job_id, sequence, progress, log_lines, lease_token
            ):
                self.calls.append(log_lines)

        client = Client()
        reporter = _JobProgressReporter(client, "worker-a", {"id": "job-a", "lease_token": "lease"})

        reporter.emit("line\n", force=True, timestamps=["2024-05-05T10:00:00Z"])
        reporter.finish()

        delivered_lines = [call for call in client.calls if call]
        self.assertEqual(delivered_lines, [["line"]])

if __name__ == "__main__":
    unittest.main()
