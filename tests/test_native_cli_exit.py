import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.app import main as backup_main


class NativeCliExitTests(unittest.TestCase):
    def test_backup_failure_raises_system_exit(self):
        with patch.dict(os.environ, {"BACKUP_RUNTIME_TYPE": "native", "BACKUP_SOURCES_JSON": '["/srv/data"]'}, clear=False), \
             patch.object(backup_main.BackupService, "execute_backup", return_value=SimpleNamespace(success=False)):
            with self.assertRaises(SystemExit) as raised:
                backup_main.main()
        self.assertEqual(raised.exception.code, 1)

    def test_restore_failure_raises_system_exit(self):
        failure = SimpleNamespace(success=False, permission_warnings=[], planned_actions=[], error="failed", duration=0)
        environment = {"BACKUP_RUNTIME_TYPE": "native", "BACKUP_SOURCES_JSON": '["/srv/data"]', "RESTORE_MODE": "1", "RESTORE_TARGET_PATH": "/srv/restore"}
        with patch.dict(os.environ, environment, clear=False), patch.object(backup_main.RestoreService, "execute_restore", return_value=failure):
            with self.assertRaises(SystemExit) as raised:
                backup_main.main()
        self.assertEqual(raised.exception.code, 1)

    def test_restore_result_write_failure_raises_system_exit(self):
        result = SimpleNamespace(success=True, permission_warnings=[], planned_actions=[], duration=0)
        environment = {"BACKUP_RUNTIME_TYPE": "native", "BACKUP_SOURCES_JSON": '["/srv/data"]', "RESTORE_MODE": "1", "RESTORE_TARGET_PATH": "/srv/restore"}
        with patch.dict(os.environ, environment, clear=False), patch.object(backup_main.RestoreService, "execute_restore", return_value=result), \
             patch.object(backup_main, "_write_restore_result", side_effect=OSError("unavailable")):
            with self.assertRaises(SystemExit) as raised:
                backup_main.main()
        self.assertEqual(raised.exception.code, 1)
