"""Durable journal for the last control-plane schedule owned by the worker offline.

The worker keeps the most recent schedule state (including the resolved
backup payloads it received) so prolonged control-plane outages continue with
the last known configuration ("Continuar con la última configuración"). The
file is written atomically with private permissions because the payloads hold
resolved credentials, mirroring the worker credential/recovery journals.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from src.control_plane.application.services.scheduler_service import _parse_cron_expression
from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore


class WorkerScheduleStateJournal:
    """Atomic, bounded store of the worker's last resolved schedule state."""

    VERSION = 1
    MAX_ENTRIES = 32
    MAX_SLOTS = 64
    MAX_ENTRY_BYTES = 256 * 1024
    MAX_RECORD_BYTES = 2 * 1024 * 1024
    MAX_TEXT_LENGTH = 256
    MAX_REVISION_LENGTH = 128
    MAX_SLOT_LENGTH = 64
    _ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

    def __init__(self, path):
        self.path = Path(path)

    @classmethod
    def _bounded_text(cls, value: Any, maximum: int) -> Optional[str]:
        if not isinstance(value, str) or not value or len(value) > maximum:
            return None
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            return None
        return value

    @classmethod
    def _valid_cron(cls, expr: Any) -> bool:
        try:
            _parse_cron_expression(expr)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False
        return isinstance(expr, str)

    @classmethod
    def _normalize_entry(cls, value: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(value, dict) or set(value) - {"target_id", "cron_expression", "enabled", "payload", "timezone"}:
            return None
        target_id = value.get("target_id")
        if not isinstance(target_id, str) or not cls._ID_PATTERN.fullmatch(target_id):
            return None
        cron = value.get("cron_expression")
        if not cls._valid_cron(cron):
            return None
        payload = value.get("payload")
        if not isinstance(payload, dict):
            return None
        entry = {"target_id": target_id, "cron_expression": cron, "payload": payload}
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            return None
        entry["enabled"] = enabled
        timezone_name = cls._bounded_text(value.get("timezone"), cls.MAX_TEXT_LENGTH)
        if timezone_name is not None:
            entry["timezone"] = timezone_name
        try:
            encoded = json.dumps(entry, separators=(",", ":"), ensure_ascii=True)
        except (TypeError, ValueError, RecursionError):
            return None
        if len(encoded.encode("utf-8")) > cls.MAX_ENTRY_BYTES:
            return None
        return entry

    @classmethod
    def _normalize_slots(cls, value: Any) -> Dict[str, str]:
        if not isinstance(value, dict):
            return {}
        slots = {}
        for key, slot in value.items():
            if not isinstance(key, str) or not cls._ID_PATTERN.fullmatch(key):
                continue
            if not isinstance(slot, str) or not slot or len(slot) > cls.MAX_SLOT_LENGTH:
                continue
            slots[key] = slot
        # Newest marks win: drop from the front once the bound is exceeded.
        items = list(slots.items())
        overflow = max(0, len(items) - cls.MAX_SLOTS)
        return dict(items[overflow:])

    @classmethod
    def normalize_state(cls, state: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(state, dict) or set(state) - {
            "version", "revision", "generated_at", "timezone", "default_cron", "entries", "slots",
        }:
            raise ValueError("invalid worker schedule state")
        if state.get("version") != cls.VERSION:
            raise ValueError("unsupported worker schedule state version")
        revision = cls._bounded_text(state.get("revision"), cls.MAX_REVISION_LENGTH)
        if revision is None:
            raise ValueError("invalid worker schedule state revision")
        entries_raw = state.get("entries")
        if not isinstance(entries_raw, list) or len(entries_raw) > cls.MAX_ENTRIES:
            raise ValueError("invalid worker schedule state entries")
        entries = []
        for item in entries_raw:
            entry = cls._normalize_entry(item)
            if entry is None:
                raise ValueError("invalid worker schedule state entry")
            entries.append(entry)
        normalized = {
            "version": cls.VERSION,
            "revision": revision,
            "entries": entries,
            "slots": cls._normalize_slots(state.get("slots", {})),
        }
        generated_at = cls._bounded_text(state.get("generated_at"), cls.MAX_TEXT_LENGTH)
        if state.get("generated_at") is not None:
            if generated_at is None:
                raise ValueError("invalid worker schedule state generated_at")
            normalized["generated_at"] = generated_at
        timezone_name = cls._bounded_text(state.get("timezone"), cls.MAX_TEXT_LENGTH)
        if state.get("timezone") is not None:
            if timezone_name is None:
                raise ValueError("invalid worker schedule state timezone")
            normalized["timezone"] = timezone_name
        default_cron = state.get("default_cron")
        if default_cron is not None:
            if not cls._valid_cron(default_cron):
                raise ValueError("invalid worker schedule state default cron")
            normalized["default_cron"] = default_cron
        try:
            encoded = json.dumps(normalized, separators=(",", ":"), ensure_ascii=True)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("worker schedule state is not serializable")
        if len(encoded.encode("utf-8")) > cls.MAX_RECORD_BYTES:
            raise ValueError("worker schedule state exceeds the permitted size")
        return normalized

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            if self.path.stat().st_size > self.MAX_RECORD_BYTES:
                return None
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return None
        try:
            return self.normalize_state(value)
        except ValueError:
            return None

    def save(self, state: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(state, dict) and "slots" not in state and isinstance(state.get("entries"), list):
            # Fetched schedule payloads carry no slots; preserve the executed-slot
            # history so a refreshed schedule never re-runs an old slot.
            previous = self.load()
            state = {**state, "slots": previous.get("slots", {}) if previous else {}}
        if isinstance(state, dict) and "version" not in state:
            # The version is a journal format detail, not part of the fetched contract.
            state = {"version": self.VERSION, **state}
        normalized = self.normalize_state(state)
        self._write(normalized)
        return normalized

    def mark_slot(self, target_id: str, slot_key: str) -> None:
        state = self.load() or {
            "version": self.VERSION,
            "revision": "offline",
            "entries": [],
        }
        state["slots"] = {
            **(state.get("slots") or {}),
            target_id: self._bounded_text(slot_key, self.MAX_SLOT_LENGTH),
        }
        if state["slots"][target_id] is None:
            raise ValueError("invalid schedule slot key")
        self._write(self.normalize_state(state))

    def completed_slots(self) -> Dict[str, str]:
        state = self.load()
        if not isinstance(state, dict):
            return {}
        return dict(state.get("slots") or {})

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def _write(self, payload: Dict[str, Any]) -> None:
        WorkerCredentialStore(self.path).write_json(self.path, payload)
