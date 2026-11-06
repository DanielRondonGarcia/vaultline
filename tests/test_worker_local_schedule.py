import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from src.control_plane.domain.models import JobStatus
from src.worker_agent.application.services.local_schedule_service import LocalScheduleService
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig, WorkerJobExecutionResult
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient, ControlPlaneHTTPError
from src.worker_agent.infrastructure.security.offline_result_outbox import OfflineResultOutbox
from src.worker_agent.infrastructure.security.schedule_state_journal import WorkerScheduleStateJournal


def _http_error(status):
    return ControlPlaneHTTPError("POST", f"/api/v1/workers/w/schedule-state/{status}", status, "detail")


def _state(revision, cron="15 3 * * *", timezone_name="UTC", payload=None):
    return {
        "revision": revision,
        "generated_at": "2025-05-05T09:00:00+00:00",
        "timezone": timezone_name,
        "entries": [
            {
                "target_id": "target-a",
                "cron_expression": cron,
                "enabled": True,
                "payload": payload
                or {"target_id": "target-a", "environment": {"RESTIC_REPOSITORY": "/repo"}},
            }
        ],
    }


def _result(status=JobStatus.SUCCEEDED, summary=None, lines=("(backup log line)",)):
    return WorkerJobExecutionResult(
        status=status,
        result_summary=summary or {"status_code": 0, "target_id": "target-a"},
        log_lines=list(lines),
    )


class _Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class WorkerLocalScheduleTests(unittest.TestCase):
    """Worker-side autonomous scheduling: S1-S4 offline execution and sync."""

    def _service(self, directory, *, fetch, deliver=None, now=None, executor=None,
                 max_records=8, journal=None):
        if deliver is None:
            deliver = _fail_deliver
        outbox = OfflineResultOutbox(Path(directory) / "worker_offline_results.json", max_records=max_records)
        return LocalScheduleService(
            journal=journal or WorkerScheduleStateJournal(Path(directory) / "worker_schedule_state.json"),
            outbox=outbox,
            fetch=fetch,
            deliver=deliver,
            executor=executor or (lambda payload, entry, slot_key: _result()),
            now=now,
        )

    def test_legacy_control_plane_responses_keep_the_last_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = WorkerScheduleStateJournal(Path(directory) / "worker_schedule_state.json")
            journal.save(_state("rev-1"))
            # A legacy control plane has no schedule route (404 -> client yields None).
            fetch = Mock(return_value=None)
            schedule = self._service(directory, fetch=fetch, journal=journal)
            self.assertEqual(schedule.refresh("worker-a"), "legacy_control_plane")
            self.assertEqual(journal.load()["revision"], "rev-1")

            # Connectivity outage also keeps the durable last configuration (S2).
            fetch = Mock(side_effect=ConnectionError("offline"))
            schedule = self._service(directory, fetch=fetch, journal=journal)
            self.assertEqual(schedule.refresh("worker-a"), "kept_last_configuration")
            self.assertEqual(journal.load()["revision"], "rev-1")

            # A legacy control plane answering an unrelated payload never replaces it.
            fetch = Mock(return_value={"id": "worker-a", "status": "online"})
            schedule = self._service(directory, fetch=fetch, journal=journal)
            self.assertEqual(schedule.refresh("worker-a"), "kept_last_configuration")
            self.assertEqual(journal.load()["revision"], "rev-1")

    def test_schedule_acknowledges_only_after_persisting_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = WorkerScheduleStateJournal(Path(directory) / "worker_schedule_state.json")
            ack = Mock(side_effect=[ConnectionError("offline"), {"acknowledged": True}])
            schedule = LocalScheduleService(
                journal=journal, outbox=OfflineResultOutbox(Path(directory) / "outbox.json"),
                fetch=Mock(return_value=_state("rev-1")), deliver=Mock(), executor=Mock(), acknowledge=ack,
            )
            schedule.refresh("worker-a")
            self.assertEqual(journal.load()["revision"], "rev-1")
            schedule.refresh("worker-a")
            self.assertEqual(ack.call_count, 2)
            ack.assert_called_with("worker-a", "rev-1")

    def test_schedule_state_fetch_persists_the_new_revision_once(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = WorkerScheduleStateJournal(Path(directory) / "worker_schedule_state.json")
            fetch = Mock(return_value=_state("rev-7"))
            schedule = self._service(directory, fetch=fetch, journal=journal)
            self.assertEqual(schedule.refresh("worker-a"), "updated")
            self.assertEqual(journal.load()["revision"], "rev-7")
            self.assertEqual(schedule.refresh("worker-a"), "unchanged")
            self.assertEqual(fetch.call_count, 2)

    def test_unavailable_timezone_fails_visibly_without_executing(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = WorkerScheduleStateJournal(Path(directory) / "worker_schedule_state.json")
            journal.save(_state("rev-1", cron="* * * * *", timezone_name="Invalid/Unknown"))
            executor = Mock(return_value=_result())
            schedule = self._service(directory, fetch=Mock(return_value=None), executor=executor)
            with self.assertRaises(ValueError):
                schedule.evaluate("worker-a")
            executor.assert_not_called()
            self.assertEqual(journal.completed_slots(), {})

    def test_long_backup_does_not_hide_other_targets_due_in_same_minute(self):
        with tempfile.TemporaryDirectory() as directory:
            now = _Clock(datetime(2025, 5, 5, 3, 15, 45, tzinfo=timezone.utc))
            state = _state("rev-1", cron="15 3 * * *")
            second = dict(state["entries"][0], target_id="target-b", payload={"target_id": "target-b"})
            state["entries"].append(second)
            journal = WorkerScheduleStateJournal(Path(directory) / "worker_schedule_state.json")
            journal.save(state)
            def slow_executor(*_args):
                now.value += timedelta(minutes=2)
                return _result()
            executor = Mock(side_effect=slow_executor)
            schedule = self._service(directory, fetch=Mock(return_value=None), now=now, executor=executor, journal=journal)
            summary = schedule.evaluate("worker-a")
            self.assertEqual(summary["executed"], 2)
            self.assertEqual(executor.call_count, 2)

    def test_reachable_legacy_control_plane_does_not_run_stale_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = WorkerScheduleStateJournal(Path(directory) / "worker_schedule_state.json")
            journal.save(_state("rev-1", cron="* * * * *"))
            executor = Mock(return_value=_result())
            schedule = self._service(directory, fetch=Mock(return_value=None), executor=executor, journal=journal)
            schedule.poll("worker-a")
            executor.assert_not_called()
            self.assertEqual(journal.completed_slots(), {})

    def test_due_backup_is_not_missed_when_control_plane_request_is_slow(self):
        with tempfile.TemporaryDirectory() as directory:
            now = _Clock(datetime(2025, 5, 5, 3, 15, 45, tzinfo=timezone.utc))
            journal = WorkerScheduleStateJournal(Path(directory) / "worker_schedule_state.json")
            journal.save(_state("rev-1", cron="15 3 * * *"))
            executor = Mock(return_value=_result())
            def delayed_fetch(_worker):
                now.value += timedelta(seconds=30)
                raise ConnectionError("offline")
            schedule = self._service(directory, fetch=delayed_fetch, now=now, executor=executor, journal=journal)
            schedule.poll("worker-a")
            executor.assert_called_once()

    def test_offline_runtime_result_does_not_persist_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = WorkerScheduleStateJournal(Path(directory) / "worker_schedule_state.json")
            journal.save(_state("rev-1", cron="* * * * *", payload={"target_id": "target-a", "environment": {"RESTIC_PASSWORD": "private-test-secret"}}))
            result = _result(summary={"detail": {"message": "private-test-secret"}}, lines=("private-test-secret",))
            schedule = self._service(directory, fetch=Mock(return_value=None), executor=Mock(return_value=result), journal=journal)
            schedule.evaluate("worker-a")
            outbox = OfflineResultOutbox(Path(directory) / "worker_offline_results.json")
            self.assertNotIn("private-test-secret", str(outbox.pending()))

    def test_offline_executor_exception_does_not_persist_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = WorkerScheduleStateJournal(Path(directory) / "worker_schedule_state.json")
            journal.save(_state("rev-1", cron="* * * * *", payload={"target_id": "target-a", "environment": {"RESTIC_PASSWORD": "private-test-secret"}}))
            def failed_executor(*_args):
                raise RuntimeError("failed with private-test-secret")
            schedule = self._service(directory, fetch=Mock(return_value=None), executor=failed_executor, journal=journal)
            schedule.evaluate("worker-a")
            outbox = OfflineResultOutbox(Path(directory) / "worker_offline_results.json")
            self.assertNotIn("private-test-secret", str(outbox.pending()))

    def test_due_cron_executes_backup_offline_and_queues_the_result_once(self):
        with tempfile.TemporaryDirectory() as directory:
            now = _Clock(datetime(2025, 5, 5, 3, 15, second=5, tzinfo=timezone.utc))
            journal = WorkerScheduleStateJournal(Path(directory) / "worker_schedule_state.json")
            journal.save(_state("rev-1", cron="15 3 * * *"))
            executions = []

            def executor(payload, entry, slot_key):
                executions.append((payload, slot_key))
                return _result()

            schedule = self._service(directory, fetch=Mock(return_value=None), now=now, executor=executor)
            summary = schedule.evaluate("worker-a")
            self.assertEqual(summary["executed"], 1)
            self.assertEqual(executions[0][0]["environment"]["RESTIC_REPOSITORY"], "/repo")
            self.assertEqual(executions[0][1], "2025-05-05T03:15:00+00:00")
            outbox = OfflineResultOutbox(Path(directory) / "worker_offline_results.json")
            [pending] = outbox.pending()
            self.assertEqual(pending["id"], "offline:target-a:2025-05-05T03:15:00+00:00")
            self.assertEqual(pending["worker_id"], "worker-a")
            self.assertEqual(pending["target_id"], "target-a")
            self.assertEqual(pending["status"], "succeeded")
            self.assertEqual(pending["result_summary"]["target_id"], "target-a")
            self.assertEqual(pending["log_lines"], ["(backup log line)"])
            self.assertIn("executed_at", pending)
            self.assertEqual(pending.get("trigger"), "offline_schedule")

            now.value = datetime(2025, 5, 5, 3, 15, second=40, tzinfo=timezone.utc)
            self.assertEqual(schedule.evaluate("worker-a")["executed"], 0)
            self.assertEqual(len(executions), 1)
            self.assertEqual(outbox.dropped_count(), 0)

    def test_new_slot_executes_again_and_observable_capacity_loss_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            now = _Clock(datetime(2025, 5, 5, 3, 15, tzinfo=timezone.utc))
            journal = WorkerScheduleStateJournal(Path(directory) / "worker_schedule_state.json")
            journal.save(_state("rev-1", cron="* * * * *"))
            executions = []

            def executor(payload, entry, slot_key):
                executions.append(slot_key)
                return _result()

            schedule = self._service(
                directory, fetch=Mock(return_value=None), now=now, executor=executor, max_records=1,
            )
            first = schedule.evaluate("worker-a")
            self.assertEqual(first["executed"], 1)
            now.value = now.value + timedelta(minutes=1)
            second = schedule.evaluate("worker-a")
            self.assertEqual(second["executed"], 1)
            self.assertEqual(len(executions), 2)
            outbox = OfflineResultOutbox(Path(directory) / "worker_offline_results.json")
            pending = outbox.pending()
            self.assertEqual([item["id"] for item in pending], [f"offline:target-a:{executions[-1]}"])
            self.assertEqual(outbox.dropped_count(), 1)
            self.assertEqual(
                schedule.poll("worker-a")["dropped_results"],
                OfflineResultOutbox(Path(directory) / "worker_offline_results.json").dropped_count(),
            )

    def test_replay_delivers_once_in_order_and_acknowledges_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox_path = Path(directory) / "worker_offline_results.json"
            outbox = OfflineResultOutbox(outbox_path)
            outbox.append({"id": "offline:target-a:t1", "worker_id": "worker-a", "status": "succeeded"})
            outbox.append({"id": "offline:target-a:t2", "worker_id": "worker-a", "status": "failed"})
            delivered = []

            def deliver(worker_id, record):
                delivered.append(record["id"])
                return {"acknowledged": True, "job_id": record["id"]}

            schedule = self._service(directory, fetch=Mock(return_value=None), deliver=deliver)
            self.assertEqual(schedule.replay("worker-a"), 2)
            self.assertEqual(delivered, ["offline:target-a:t1", "offline:target-a:t2"])
            self.assertEqual(outbox.pending(), [])
            self.assertEqual(schedule.replay("worker-a"), 0)
            self.assertEqual(len(delivered), 2)

    def test_replay_retains_result_if_control_plane_does_not_acknowledge_it(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = OfflineResultOutbox(Path(directory) / "worker_offline_results.json")
            outbox.append({"id": "offline:target-a:t1", "worker_id": "worker-a", "status": "succeeded"})
            schedule = self._service(directory, fetch=Mock(return_value=None), deliver=Mock(return_value={}))
            self.assertEqual(schedule.replay("worker-a"), 0)
            self.assertEqual(outbox.pending()[0]["id"], "offline:target-a:t1")

    def test_replay_delivers_later_results_when_one_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = OfflineResultOutbox(Path(directory) / "worker_offline_results.json")
            outbox.append({"id": "offline:target-a:t1", "worker_id": "worker-a", "status": "succeeded"})
            outbox.append({"id": "offline:target-b:t2", "worker_id": "worker-a", "status": "succeeded"})
            def deliver(_worker_id, record):
                if record["id"] == "offline:target-a:t1":
                    raise _http_error(400)
                return {"acknowledged": True, "job_id": record["id"]}
            schedule = self._service(directory, fetch=Mock(return_value=None), deliver=deliver)
            self.assertEqual(schedule.replay("worker-a"), 1)
            self.assertEqual([r["id"] for r in outbox.pending()], ["offline:target-a:t1"])

    def test_replay_keeps_rejected_results_until_acknowledged(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox_path = Path(directory) / "worker_offline_results.json"
            outbox = OfflineResultOutbox(outbox_path)
            outbox.append({"id": "offline:target-a:t1", "worker_id": "worker-a", "status": "succeeded"})

            schedule = self._service(
                directory,
                fetch=Mock(return_value=None),
                deliver=Mock(side_effect=_http_error(404)),
            )
            # A legacy control plane keeps the result pending for a future upgrade.
            self.assertEqual(schedule.replay("worker-a"), 0)
            self.assertEqual(outbox.pending()[0]["id"], "offline:target-a:t1")

            schedule = self._service(
                directory,
                fetch=Mock(return_value=None),
                deliver=Mock(side_effect=ConnectionError("offline")),
            )
            self.assertEqual(schedule.replay("worker-a"), 0)
            self.assertEqual(outbox.pending()[0]["id"], "offline:target-a:t1")

            deliver = Mock(side_effect=_http_error(409))
            schedule = self._service(directory, fetch=Mock(return_value=None), deliver=deliver)
            self.assertEqual(schedule.replay("worker-a"), 0)
            self.assertEqual(outbox.pending()[0]["id"], "offline:target-a:t1")

    def test_main_polls_local_schedule_before_control_plane_heartbeat(self):
        import os
        from unittest.mock import patch
        from src.worker_agent import main as worker_main
        service = Mock()
        service.config = WorkerAgentConfig("http://cp", "worker", "host", worker_id="worker-a")
        service.ensure_registered.return_value = "worker-a"
        service.cleanup_orphaned_runtime_containers.return_value = {}
        service.poll_interactive_once.return_value = []
        order = []
        service.poll_local_schedule.side_effect = lambda *_args: order.append("schedule") or {}
        service.send_heartbeat.side_effect = lambda: order.append("heartbeat")
        with patch.object(worker_main, "build_service", return_value=service), \
             patch.object(worker_main, "start_health_server"), \
             patch.dict(os.environ, {"WORKER_RUN_ONCE": "true", "WORKER_HEALTH_PORT": "0"}, clear=False):
            worker_main.main()
        self.assertIn("schedule", order)
        self.assertIn("heartbeat", order)
        self.assertLess(order.index("schedule"), order.index("heartbeat"))

    def test_worker_execute_offline_backup_uses_the_normal_job_execution_path(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Mock()
            runtime.run_runtime_job.return_value = {
                "success": True,
                "status_code": 0,
                "logs": "backup done\n",
                "stderr": "",
            }
            client = Mock()
            client.is_job_cancelled = Mock(return_value=False)
            service = WorkerAgentService(
                WorkerAgentConfig("http://cp", "worker", "host", worker_id="worker-a"),
                client,
                runtime,
                recovery_file=str(Path(directory) / "recovery.json"),
            )
            payload = {
                "target_id": "target-a",
                "backup_strategy": "restic",
                "environment": {"RESTIC_PASSWORD": "secret-password"},
            }
            execution = service.execute_offline_backup(payload, "offline-schedule-key")
            self.assertEqual(execution.status, JobStatus.SUCCEEDED)
            [call] = runtime.run_runtime_job.call_args_list
            self.assertEqual(call.kwargs["payload"]["target_id"], "target-a")
            self.assertFalse(client.is_job_cancelled.called)

    def test_worker_poll_local_schedule_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Mock()
            runtime.run_runtime_job.return_value = {
                "success": True,
                "status_code": 0,
                "logs": "backup done\n",
                "stderr": "",
            }
            client = Mock()
            client.download_schedule_state = Mock(side_effect=ConnectionError("offline"))
            service = WorkerAgentService(
                WorkerAgentConfig("http://cp", "worker", "host", worker_id="worker-a"),
                client,
                runtime,
                recovery_file=str(Path(directory) / "recovery.json"),
            )
            journal = WorkerScheduleStateJournal(Path(directory) / "worker_schedule_state.json")
            journal.save(_state("rev-1", cron=f"{datetime.now(timezone.utc).minute} * * * *"))
            now = _Clock(datetime.now(timezone.utc).replace(second=30))
            service.local_schedule = LocalScheduleService(
                journal=journal,
                outbox=OfflineResultOutbox(Path(directory) / "worker_offline_results.json"),
                fetch=client.download_schedule_state,
                deliver=client.submit_offline_result,
                executor=service.execute_offline_backup,
                now=now,
            )
            summary = service.poll_local_schedule("worker-a")
            self.assertEqual(summary["offline"]["executed"], 1)
            self.assertEqual(summary["replayed"], 0)
            runtime.run_runtime_job.assert_called_once()
            outbox = OfflineResultOutbox(Path(directory) / "worker_offline_results.json")
            self.assertEqual(len(outbox.pending()), 1)
            self.assertEqual(outbox.pending()[0]["status"], JobStatus.SUCCEEDED)
            slots = journal.completed_slots()
            [slot] = slots.values()
            again = service.poll_local_schedule("worker-a")
            self.assertEqual(again["offline"]["executed"], 0)
            self.assertEqual(outbox.dropped_count(), 0)
            self.assertEqual(slots, journal.completed_slots())

    def test_control_plane_client_downgrades_legacy_schedule_responses(self):
        client = ControlPlaneClient("http://cp", timeout_seconds=1)

        def legacy_post(path, payload, **kwargs):
            self.assertIn("/schedule-state", path)
            raise _http_error(404)

        client._post = legacy_post
        self.assertIsNone(client.download_schedule_state("worker-a"))

        def error_post(path, payload, **kwargs):
            raise _http_error(500)

        client._post = error_post
        with self.assertRaises(ControlPlaneHTTPError):
            client.download_schedule_state("worker-a")

        captured = {}

        def submit_post(path, payload, **kwargs):
            captured["path"] = path
            captured["payload"] = payload
            return {}

        client._post = submit_post
        client.submit_offline_result("worker-a", {"id": "offline:x", "status": "succeeded"})
        self.assertIn("/offline-results", captured["path"])
        self.assertIn("result", captured["payload"])


def _fail_deliver(worker_id, record):
    raise AssertionError("deliver must not be called by this test")


if __name__ == "__main__":
    unittest.main()
