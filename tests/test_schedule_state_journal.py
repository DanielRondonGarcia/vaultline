import os
import tempfile
import unittest
from pathlib import Path

from src.worker_agent.infrastructure.security.schedule_state_journal import (
    WorkerScheduleStateJournal,
)


def _entry(**overrides):
    entry = {
        "target_id": "target-a",
        "cron_expression": "15 3 * * *",
        "enabled": True,
        "payload": {
            "target_id": "target-a",
            "environment": {"RESTIC_REPOSITORY": "/repo", "RESTIC_PASSWORD": "resolved-secret"},
        },
    }
    entry.update(overrides)
    return entry


def _state(**overrides):
    state = {
        "revision": "rev-1",
        "generated_at": "2025-05-05T09:00:00+00:00",
        "timezone": "UTC",
        "entries": [_entry()],
    }
    state.update(overrides)
    return state


class WorkerScheduleStateJournalTests(unittest.TestCase):
    """Durable, secret-safe persistence of the last control-plane schedule."""

    def test_valid_schedule_state_survives_restart_with_resolved_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker_schedule_state.json"
            WorkerScheduleStateJournal(path).save(_state())
            restarted = WorkerScheduleStateJournal(path).load()
            self.assertIsNotNone(restarted)
            self.assertEqual(restarted["revision"], "rev-1")
            self.assertEqual(restarted["timezone"], "UTC")
            self.assertEqual(len(restarted["entries"]), 1)
            entry = restarted["entries"][0]
            self.assertEqual(entry["target_id"], "target-a")
            self.assertEqual(entry["cron_expression"], "15 3 * * *")
            self.assertTrue(entry["enabled"])
            self.assertEqual(entry["payload"]["environment"]["RESTIC_PASSWORD"], "resolved-secret")

    def test_completed_slots_are_durable_idempotent_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker_schedule_state.json"
            journal = WorkerScheduleStateJournal(path)
            journal.save(_state())
            journal.mark_slot("target-a", "2025-05-05T03:15:00+00:00")
            journal.mark_slot("target-a", "2025-05-05T03:15:00+00:00")
            self.assertEqual(
                journal.completed_slots(),
                {"target-a": "2025-05-05T03:15:00+00:00"},
            )
            self.assertEqual(
                WorkerScheduleStateJournal(path).load()["entries"][0]["target_id"],
                "target-a",
            )
            for index in range(70):
                journal.mark_slot(f"target-{index:03d}", "2025-05-05T04:00:00+00:00")
            slots = journal.completed_slots()
            self.assertEqual(len(slots), WorkerScheduleStateJournal.MAX_SLOTS)
            self.assertNotIn("target-a", slots)
            self.assertIn("target-069", slots)

    def test_corrupt_or_invalid_state_is_never_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker_schedule_state.json"
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("{not json")
            self.assertIsNone(WorkerScheduleStateJournal(path).load())

            journal = WorkerScheduleStateJournal(path)
            journal.save(_state())
            with open(path, "r+b") as stream:
                stream.truncate(4)
            self.assertIsNone(WorkerScheduleStateJournal(path).load())

            path.write_text("{}", encoding="utf-8")
            self.assertIsNone(WorkerScheduleStateJournal(path).load())

    def test_invalid_cron_and_legacy_control_plane_payloads_are_rejected_before_persisting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker_schedule_state.json"
            journal = WorkerScheduleStateJournal(path)
            with self.assertRaises(ValueError):
                journal.save(_state(revision="rev-2", entries=[_entry(cron_expression="every night")]))
            with self.assertRaises(ValueError):
                # A legacy control plane answering an unrelated payload (heartbeat-like shape).
                journal.save({"id": "worker-1", "status": "online", "labels": {}})
            self.assertFalse(path.exists() and WorkerScheduleStateJournal(path).load() is not None)

            journal.save(_state(revision="rev-1"))
            # An empty entries list is legitimate: a worker with nothing scheduled.
            journal.save(_state(revision="rev-3", entries=[]))
            for bad in (
                _state(revision="rev-4", entries=[_entry(target_id="in/valid")]),
                _state(revision="rev-5", entries=[_entry(payload="not-a-dict")]),
                _state(revision="rev-6", entries=[_entry(enabled="yes")]),
                _state(revision="rev-7", timezone="not a controlled string" * 50),
            ):
                with self.assertRaises(ValueError):
                    journal.save(bad)
            self.assertEqual(journal.load()["revision"], "rev-3")

    def test_state_file_is_written_with_private_permissions(self):
        if os.name == "nt":
            self.skipTest("POSIX permission check")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker_schedule_state.json"
            WorkerScheduleStateJournal(path).save(_state())
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)

    def test_schedule_journal_uses_worker_credential_store_acl(self):
        from unittest.mock import patch
        from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker_schedule_state.json"
            with patch.object(WorkerCredentialStore, "write_json", autospec=True, wraps=WorkerCredentialStore.write_json) as write:
                WorkerScheduleStateJournal(path).save(_state())
                self.assertTrue(write.called, "schedule secrets must use the protected credential store")

    def test_clear_removes_the_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker_schedule_state.json"
            journal = WorkerScheduleStateJournal(path)
            journal.save(_state())
            journal.clear()
            self.assertIsNone(journal.load())
            self.assertIsNone(journal.load())


if __name__ == "__main__":
    unittest.main()
