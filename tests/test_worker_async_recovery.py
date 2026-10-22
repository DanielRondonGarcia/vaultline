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
                    self.saved = service.terminal_report_journal.load()
                    return {"status": kwargs["status"]}
            client = Client()
            runtime = Mock()
            runtime.run_runtime_job.return_value = {"success": True, "status_code": 0, "logs": "done", "stderr": ""}
            service = WorkerAgentService(
                WorkerAgentConfig("http://cp", "worker", "host", worker_id="worker-a"),
                client, runtime, recovery_file=str(Path(directory) / "recovery.json"),
            )
            service._process_jobs("worker-a", [{"id": "job-a", "command": "backup.run", "lease_token": "lease", "payload": {"environment": {}}}])
            self.assertEqual(len(client.saved), 1)
            self.assertEqual(client.saved[0]["status"], "succeeded")
            self.assertEqual(service.terminal_report_journal.load(), [])


if __name__ == "__main__":
    unittest.main()
