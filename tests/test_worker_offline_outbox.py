import tempfile
import unittest
from pathlib import Path

from src.worker_agent.infrastructure.security.offline_result_outbox import OfflineResultOutbox


class OfflineResultOutboxTests(unittest.TestCase):
    def test_records_survive_restart_and_acknowledge_by_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.json"
            outbox = OfflineResultOutbox(path, max_records=2)
            outbox.append({"id": "first", "status": "succeeded"})
            outbox.append({"id": "second", "status": "failed"})
            restarted = OfflineResultOutbox(path, max_records=2)
            self.assertEqual([r["id"] for r in restarted.pending()], ["first", "second"])
            restarted.acknowledge("first")
            self.assertEqual([r["id"] for r in OfflineResultOutbox(path, max_records=2).pending()], ["second"])

    def test_overflow_discards_oldest_and_persists_loss_count(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.json"
            outbox = OfflineResultOutbox(path, max_records=2)
            for name in ("first", "second", "third"):
                outbox.append({"id": name, "status": "succeeded"})
            restarted = OfflineResultOutbox(path, max_records=2)
            self.assertEqual([r["id"] for r in restarted.pending()], ["second", "third"])
            self.assertEqual(restarted.dropped_count(), 1)

    def test_duplicate_id_does_not_replace_result(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = OfflineResultOutbox(Path(directory) / "outbox.json", max_records=2)
            outbox.append({"id": "first", "status": "succeeded"})
            with self.assertRaises(ValueError):
                outbox.append({"id": "first", "status": "failed"})
            self.assertEqual(outbox.pending(), [{"id": "first", "status": "succeeded"}])


if __name__ == "__main__":
    unittest.main()
