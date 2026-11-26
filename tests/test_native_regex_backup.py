import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.app.domain.models import BackupConfig
from src.app.infrastructure.adapters.backup_strategy import ResticBackupStrategy
from src.native_file_filter import filter_scope_tag

from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter


ARCHIVE_REGEX = r"vzdump-(?:qemu|lxc)-(?:100|105)-[0-9_]+-[0-9_]+\.(?:vma|tar)\.zst"


@unittest.skipUnless(shutil.which("restic"), "restic executable is unavailable")
class NativeRegexBackupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / "Proxmox dump"
        self.source.mkdir()
        self.repository = self.root / "repo"
        self.worker = NativeRuntimeAdapter(timeout_seconds=20)

    def restic(self, *args):
        result = subprocess.run(
            ["restic", "--repo", str(self.repository), *args],
            env={**os.environ, "RESTIC_PASSWORD": "regex-test-password"}, capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def run_native(self, pattern=ARCHIVE_REGEX, *, target="proxmox-tasks", command=None, **options):
        environment = {
            "RESTIC_REPOSITORY": str(self.repository), "RESTIC_PASSWORD": "regex-test-password",
            "RESTIC_RETENTION_POLICY_CONFIGURED": "true", "RESTIC_KEEP_LAST": "8",
            "RESTIC_PRUNE": "false", **options,
        }
        if pattern is not None:
            environment["BACKUP_NATIVE_FILE_REGEX"] = pattern
        logs = []
        result = self.worker.run_runtime_job("", {
            "runtime_type": "native", "target_id": target,
            "filesystem_paths": [str(self.source)], "command": command,
            "environment": environment,
        }, output_callback=logs.append)
        return result, "\n".join(logs)

    def populate(self):
        good = self.source / "vzdump-qemu-100-2026_11_22-00_00_00.vma.zst"
        good.write_bytes(b"VM-100 original")
        lxc = self.source / "vzdump-lxc-105-2026_11_22-00_00_00.tar.zst"
        lxc.write_bytes(b"LXC-105 original")
        unrelated = self.source / "vzdump-qemu-101-2026_11_22-00_00_00.vma.zst"
        unrelated.write_bytes(b"VM-101 must never change")
        notes = good.with_suffix(".zst.notes")
        notes.write_text("Do not include this sidecar", encoding="utf-8")
        directory = self.source / "vzdump-qemu-100-2026_11_23-00_00_00.vma.zst"
        directory.mkdir()
        (directory / "must-not-be-backed-up").touch()
        return good, lxc, unrelated, notes, directory

    def test_only_matching_regular_files_are_logged_and_restored_in_place(self):
        good, lxc, unrelated, notes, directory = self.populate()
        result, logs = self.run_native()
        self.assertEqual(result.get("status_code"), 0, (result, logs))
        self.assertIn(good.name, logs)
        self.assertIn(lxc.name, logs)
        self.assertNotIn(unrelated.name, logs)
        self.assertNotIn(notes.name, logs)
        snapshot, = json.loads(self.restic("snapshots", "--json"))
        contents = [json.loads(line) for line in self.restic("ls", "--json", snapshot["id"]).splitlines()]
        self.assertEqual({node["name"] for node in contents if node.get("type") == "file"}, {good.name, lxc.name})

        good.unlink()
        lxc.write_bytes(b"already present")
        restore_options = {
            "RESTORE_MODE": "true", "RESTORE_DRY_RUN": "false", "RESTORE_STOP_CONTAINERS": "false",
            "RESTORE_TARGET_PATH": str(self.source), "RESTORE_SOURCE": snapshot["id"],
        }
        # Even after removing the source filter, a filtered snapshot must not clear the destination.
        refused, refusal_logs = self.run_native(pattern=None, RESTORE_FORCE_OVERWRITE="true", **restore_options)
        self.assertEqual(refused.get("status_code"), 1, (refused, refusal_logs))
        self.assertIn("Filtered native snapshots require preserve mode", refusal_logs)
        self.assertFalse(good.exists())
        self.assertEqual(unrelated.read_bytes(), b"VM-101 must never change")

        result, logs = self.run_native(pattern=None, **restore_options)
        self.assertEqual(result.get("status_code"), 0, (result, logs))
        self.assertEqual(good.read_bytes(), b"VM-100 original")
        self.assertEqual(lxc.read_bytes(), b"already present")
        self.assertEqual(unrelated.read_bytes(), b"VM-101 must never change")
        self.assertEqual(notes.read_text(encoding="utf-8"), "Do not include this sidecar")
        self.assertTrue((directory / "must-not-be-backed-up").is_file())

    def test_changed_matches_use_one_retention_group_without_touching_other_targets(self):
        good, lxc, unrelated, *_ = self.populate()
        other, logs = self.run_native(r"vzdump-qemu-101-.*\.vma\.zst", target="other-target")
        self.assertEqual(other.get("status_code"), 0, (other, logs))
        result, logs = self.run_native()
        self.assertEqual(result.get("status_code"), 0, (result, logs))
        self.assertIn(good.name, logs)
        good.unlink()
        new = self.source / "vzdump-qemu-100-2026_11_24-00_00_00.vma.zst"
        new.write_bytes(b"next backup")
        result, logs = self.run_native()
        self.assertEqual(result.get("status_code"), 0, (result, logs))
        self.assertIn(new.name, logs)
        # The fresh scan can never re-add the removed file; restic's retention
        # table may still print it as a path of the retained historical
        # snapshot, so only the backup phase of the run is checked.
        backup_phase = logs.split("Applying Restic retention rules")[0]
        self.assertNotIn(good.name, backup_phase)
        self.assertEqual(len(json.loads(self.restic("snapshots", "--json"))), 3)

        result, logs = self.run_native(command="restic forget --keep-last 1")
        self.assertEqual(result.get("status_code"), 0, (result, logs))
        snapshots = json.loads(self.restic("snapshots", "--json"))
        self.assertEqual({frozenset(Path(path).name for path in row["paths"]) for row in snapshots}, {
            frozenset({unrelated.name}), frozenset({new.name, lxc.name}),
        })

    def test_filtered_engine_uses_resolved_restic_for_backup_and_retention(self):
        self.populate()
        config = BackupConfig(
            source_paths=[str(self.source)], restic_repository=str(self.repository),
            restic_password="regex-test-password", native_file_regex=ARCHIVE_REGEX,
            native_file_filter_tag=filter_scope_tag("packaged-target"), restic_prune=False,
        )
        with patch("src.app.infrastructure.adapters.backup_strategy._require_executable", return_value=shutil.which("restic")):
            result = ResticBackupStrategy().perform_backup(config)
        self.assertTrue(result.success, result.error)
        snapshot, = json.loads(self.restic("snapshots", "--json"))
        self.assertEqual(snapshot["tags"], [filter_scope_tag("packaged-target")])

    def test_no_matches_or_invalid_pattern_never_initializes_repository(self):
        self.populate()
        for pattern, message in (
            (r"nothing-to-backup", "Native file regex matched no regular files"),
            (r"[", "Invalid native file regex"),
        ):
            with self.subTest(pattern=pattern):
                result, logs = self.run_native(pattern)
                self.assertEqual(result.get("status_code"), 1, (result, logs))
                self.assertIn(message, logs)
                self.assertFalse(self.repository.exists())

    def test_mixed_version_marker_unwraps_to_backup_and_scoped_forget(self):
        marker = "vaultline-native-file-filter"
        good, lxc, *_ = self.populate()
        result, logs = self.run_native(command=f"{marker} /root/backup.sh")
        self.assertEqual(result.get("status_code"), 0, (result, logs))
        self.assertIn(good.name, logs)
        other, other_logs = self.run_native(r"vzdump-lxc-105-.*\.tar\.zst", target="other-target")
        self.assertEqual(other.get("status_code"), 0, (other, other_logs))

        result, logs = self.run_native(command=f"{marker} restic forget --keep-last 1")
        self.assertEqual(result.get("status_code"), 0, (result, logs))
        snapshots = json.loads(self.restic("snapshots", "--json"))
        # Scoped group-by host,tags: an unscoped keep-last 1 would have pruned
        # the older proxmox-tasks snapshot and kept only the other-target one.
        self.assertEqual(
            {frozenset(Path(path).name for path in row["paths"]) for row in snapshots},
            {frozenset({good.name, lxc.name}), frozenset({lxc.name})},
        )

    def test_marker_requires_regex_and_inner_native_backup_or_forget_commands(self):
        self.populate()
        marker = "vaultline-native-file-filter"
        for command, message in (
            (f"{marker} /root/backup.sh", "require BACKUP_NATIVE_FILE_REGEX"),
            (f"{marker} restic forget --keep-last 1", "require BACKUP_NATIVE_FILE_REGEX"),
        ):
            with self.subTest(command=command):
                result, logs = self.run_native(pattern=None, command=command)
                self.assertEqual(result.get("status_code"), 1, (result, logs))
                self.assertIn(message, result.get("error") or "")
                self.assertFalse(self.repository.exists())
        for command, message in (
            (f"{marker} restic snapshots --json", "only allow the native backup default"),
            # RuntimeCommandPolicy.validate rejects unknown executables first;
            # either way the marked custom command never runs.
            (f"{marker} /bin/custom-script.sh", "unsupported runtime executable"),
        ):
            with self.subTest(command=command):
                result, logs = self.run_native(command=command)
                self.assertEqual(result.get("status_code"), 1, (result, logs))
                self.assertIn(message, result.get("error") or "")
                self.assertFalse(self.repository.exists())

    def test_unmarked_forget_without_regex_stays_unscoped(self):
        self.populate()
        first, *_ = self.run_native(pattern=None)
        second, *_ = self.run_native(pattern=None)
        self.assertEqual(second.get("status_code"), 0)
        result, logs = self.run_native(pattern=None, command="restic forget --keep-last 1")
        self.assertEqual(result.get("status_code"), 0, (result, logs))
        snapshots = json.loads(self.restic("snapshots", "--json"))
        self.assertEqual(len(snapshots), 1)
        self.assertNotIn(
            "vaultline-file-filter-", json.dumps([row.get("tags") for row in snapshots])
        )
