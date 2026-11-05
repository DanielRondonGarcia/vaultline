import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.app import main as runtime_main
from src.app.domain.models import BackupResult, RestoreConfig, RestoreResult
from src.app.infrastructure.adapters.backup_strategy import ResticBackupStrategy
from src.app.infrastructure.adapters.storage.multi_storage_adapter import MultiStorageAdapter


class ResticOnlyStandaloneRuntimeTests(unittest.TestCase):
    """T4 (S1, S3): the standalone application is Restic-only. The removed
    TAR strategy must not be reachable as a default or as a fallback: an
    explicit selection (BACKUP_STRATEGY / RESTORE_BACKUP_STRATEGY) of any
    other strategy must be rejected with a clear error before anything runs.
    """

    BACKUP_ENV = {
        "BACKUP_RUNTIME_TYPE": "native",
        "BACKUP_STOP_CONTAINERS": "false",
        "BACKUP_SOURCES": "/srv/data",
    }

    RESTORE_ENV = {
        "RESTORE_MODE": "true",
        "BACKUP_RUNTIME_TYPE": "native",
        "RESTORE_STOP_CONTAINERS": "false",
        "RESTORE_TARGET_PATH": "/restore/native-target",
        "BACKUP_SOURCES": "/srv/data",
    }

    def run_backup_main(self, env):
        recorded = {}

        class RecordingBackupService:
            def __init__(self, *args, **kwargs):
                recorded.update(kwargs)

            def execute_backup(self):
                return BackupResult(datetime.now(), 0, 0, True)

        with patch.dict(os.environ, env, clear=False), patch.object(
            runtime_main, "BackupService", RecordingBackupService
        ), patch.object(runtime_main, "MultiStorageAdapter"), patch.object(runtime_main, "InfluxNotifier"):
            os.environ.pop("BACKUP_STRATEGY", None)
            os.environ.pop("BACKUP_FILENAME", None)
            runtime_main.main()
        return recorded

    def run_restore_main(self, env):
        recorded = {}

        class RecordingRestoreService:
            def __init__(self, *args, **kwargs):
                recorded.update(kwargs)

            def execute_restore(self):
                return RestoreResult(datetime.now(), 0, True, planned_actions=[], permission_warnings=[])

        with patch.dict(os.environ, env, clear=False), patch.object(
            runtime_main, "RestoreService", RecordingRestoreService
        ), patch.object(runtime_main, "MultiStorageAdapter"):
            os.environ.pop("BACKUP_STRATEGY", None)
            os.environ.pop("RESTORE_BACKUP_STRATEGY", None)
            runtime_main.main()
        return recorded

    def test_default_backup_strategy_is_restic_without_explicit_selection(self):
        recorded = self.run_backup_main(dict(self.BACKUP_ENV))

        strategy = recorded["backup_strategy"]
        self.assertIsInstance(strategy, ResticBackupStrategy)
        self.assertEqual(recorded["backup_config"].backup_strategy, "restic")

    def test_default_restore_strategy_is_restic_without_explicit_selection(self):
        recorded = self.run_restore_main(dict(self.RESTORE_ENV))

        self.assertIsInstance(recorded["restore_strategy"], ResticBackupStrategy)
        self.assertEqual(recorded["restore_config"].backup_strategy, "restic")

    def assert_clear_tar_rejection(self, raised):
        message = str(raised.exception)
        self.assertIn("restic", message.lower())
        self.assertIn("tar", message.lower())
        self.assertLess(len(message), 400)

    def assert_selection_rejected(self, env):
        restore_service = Mock()
        restore_service.execute_restore.return_value = RestoreResult(
            datetime.now(), 0, True, planned_actions=[], permission_warnings=[]
        )
        with patch.dict(os.environ, env, clear=False), patch.object(
            runtime_main, "BackupService", Mock()
        ), patch.object(
            runtime_main, "RestoreService", Mock(return_value=restore_service)
        ), patch.object(runtime_main, "MultiStorageAdapter"), patch.object(
            runtime_main, "InfluxNotifier"
        ):
            with self.assertRaises(ValueError) as raised:
                runtime_main.main()
        self.assert_clear_tar_rejection(raised)

    def test_tar_backup_strategy_is_rejected_instead_of_falling_back(self):
        self.assert_selection_rejected(dict(self.BACKUP_ENV, BACKUP_STRATEGY="tar"))

    def test_unknown_backup_strategy_is_rejected_instead_of_falling_back(self):
        self.assert_selection_rejected(dict(self.BACKUP_ENV, BACKUP_STRATEGY="legacy-tarball"))

    def test_tar_restore_strategy_is_rejected_instead_of_falling_back(self):
        self.assert_selection_rejected(dict(self.RESTORE_ENV, RESTORE_BACKUP_STRATEGY="tar"))


class ResticOnlyCandidateListingTests(unittest.TestCase):
    """T4 follow-up (S1, S3): the public restore-candidate listing is
    Restic-only. Legacy raw TAR archive sources (local archives, S3, SCP,
    rclone objects, Glacier markers) must not be offered as restore
    candidates, and any unsupported strategy must be rejected instead of
    silently listing fallback raw files."""

    def candidates(self, config):
        snapshots = json.dumps([{"short_id": "snap-1", "time": "2026-01-10T12:00:00Z"}])
        runner = Mock(return_value=SimpleNamespace(stdout=snapshots))
        env = {"PATH": os.environ.get("PATH", "")}
        with patch.dict(os.environ, env, clear=False), patch(
            "src.app.infrastructure.adapters.storage.multi_storage_adapter.resolve_restic_executable",
            return_value="/bundle/restic",
        ), patch(
            "src.app.infrastructure.adapters.storage.multi_storage_adapter.subprocess.run", runner
        ):
            for name in ("RESTIC_REPOSITORY", "AWS_S3_BUCKET_NAME", "AWS_GLACIER_VAULT_NAME", "SCP_HOST", "RCLONE_REMOTE", "BACKUP_ARCHIVE"):
                os.environ.pop(name, None)
            return MultiStorageAdapter().list_restore_candidates(config)

    def test_listing_offers_only_restic_snapshots_never_raw_tar_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            pathlib_tar = Path(directory) / "legacy.tar.gz"
            pathlib_tar.write_bytes(b"obsolete tar archive")
            config = RestoreConfig(
                target_path="/restore",
                backup_strategy="restic",
                restic_repository="local:/repo",
                local_archive_path=directory,
                rclone_remote="remote:/backups",
                scp_host="legacy-host",
            )

            candidates = self.candidates(config)

        self.assertEqual([(candidate.source, candidate.strategy) for candidate in candidates], [("snap-1", "restic")])

    def test_listing_rejects_unsupported_strategies_instead_of_falling_back(self):
        for strategy in ("tar", "legacy-tarball"):
            with self.subTest(strategy=strategy):
                config = RestoreConfig(
                    target_path="/restore",
                    backup_strategy=strategy,
                    local_archive_path=str(Path("archive") / "does-not-matter.tar.gz"),
                )
                with self.assertRaises(ValueError) as raised:
                    self.candidates(config)
                message = str(raised.exception)
                self.assertIn("restic", message.lower())
                self.assertIn("tar", message.lower())


if __name__ == "__main__":
    unittest.main()
