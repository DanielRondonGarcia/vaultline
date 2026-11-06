"""Durable queue for worker-originated results awaiting control-plane delivery."""

import copy
import json
import threading
from pathlib import Path

from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore


class OfflineResultOutbox:
    """Keep the newest bounded set of results and record how many were dropped.

    Only a single worker process may own a given outbox. Callers must redact
    secrets before passing records to this store.
    """

    def __init__(self, path, *, max_records=32):
        if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        self.path = Path(path)
        self.max_records = max_records
        self._store = WorkerCredentialStore(self.path)
        self._lock = threading.RLock()

    def _read(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "dropped_count": 0, "records": []}
        if (not isinstance(state, dict) or state.get("version") != 1
                or not isinstance(state.get("records"), list)
                or not isinstance(state.get("dropped_count"), int)
                or state["dropped_count"] < 0):
            raise ValueError("invalid offline result outbox")
        return state

    def append(self, record):
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
            raise ValueError("offline result must have a nonempty id")
        with self._lock:
            state = self._read()
            if any(item.get("id") == record["id"] for item in state["records"]):
                raise ValueError("offline result id already exists")
            state["records"].append(copy.deepcopy(record))
            excess = max(0, len(state["records"]) - self.max_records)
            if excess:
                state["records"] = state["records"][excess:]
                state["dropped_count"] += excess
            self._store.write_json(self.path, state)

    def pending(self):
        with self._lock:
            return copy.deepcopy(self._read()["records"])

    def acknowledge(self, record_id):
        with self._lock:
            state = self._read()
            state["records"] = [item for item in state["records"] if item.get("id") != record_id]
            self._store.write_json(self.path, state)

    def dropped_count(self):
        with self._lock:
            return self._read()["dropped_count"]
