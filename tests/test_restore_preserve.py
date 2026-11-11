"""Preserve-mode restore and original-path (S1, S4, S5, S6) backend coverage.

"Conservar archivos existentes" (force_overwrite=false) restores only missing
items: existing files, directories, metadata, and symlinks are never followed,
replaced, or deleted — including under check/create races, and only the exact
configured native source subtree is restored on the original route (S4). A
native custom destination receives every configured source's content directly
inside it (S6); only filesystem-root destinations keep the full recorded tree.
"""

import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.app.application.services.restore_service import RestoreService
from src.app.domain.models import RestoreCandidate, RestoreConfig, RestoreResult
from src.app.infrastructure.adapters import backup_strategy
from src.app.infrastructure.adapters.backup_strategy import ResticBackupStrategy


def _make_result(success=True):
    result = RestoreResult(datetime.now(), 0, success, planned_actions=[])
    result.destructive_state = "none"
    result.normalization = {"state": "preserved", "changed": False, "unsupported_metadata": []}
    return result


def _fresh_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def _fill_tree(root: Path, contents: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, body in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def _ls_stream(nodes_by_filter, filter_arg):
    nodes = [json.dumps({**node, "struct_type": "node"}) for node in (nodes_by_filter or {}).get(filter_arg, [])]
    lines = [json.dumps({"message_type": "snapshot", "struct_type": "snapshot"})] + nodes
    return ("\n".join(lines) + "\n").encode("utf-8")


def _fake_ls_process(payload: bytes, returncode: int = 0, stderr: bytes = b""):
    return SimpleNamespace(
        stdout=io.BytesIO(payload),
        stderr=io.BytesIO(stderr),
        poll=lambda: returncode,
        returncode=returncode,
        wait=lambda timeout=None: returncode,
        pid=0,
    )


@contextlib.contextmanager
def use_restic_stub(nodes_by_filter=None, fill_staging=None, restore_calls=None, ls_payload_by_filter=None):
    """Bounded in-memory restic runtime: `ls` runs through subprocess.Popen with
    a finite BytesIO pipe; `restore` runs through subprocess.run and stages files
    into the target it receives."""

    def run(command, env=None, check=False, **kwargs):
        if command[1] == "restore":
            if restore_calls is not None:
                restore_calls.append(list(command))
            target = Path(command[command.index("--target") + 1])
            if fill_staging is not None:
                fill_staging(target)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected restic command: {command}")

    run_stub = Mock(side_effect=run)

    def popen(command, env=None, **kwargs):
        if command[1] == "ls":
            payloads = list((ls_payload_by_filter or {}).items())
            payload = payloads[0][1] if payloads else _ls_stream(nodes_by_filter, command[-1])
            return _fake_ls_process(payload)
        raise AssertionError(f"unexpected restic Popen command: {command}")

    popen_stub = Mock(side_effect=popen)
    with patch.object(backup_strategy.subprocess, "run", run_stub), patch.object(
        backup_strategy.subprocess, "Popen", popen_stub
    ), patch.object(
        # Hosts without a real restic on PATH (CI runners) must still exercise
        # the stubbed strategy; no real restic process can be spawned by these
        # stubs.
        backup_strategy, "_require_executable", return_value="restic"
    ):
        yield run_stub, popen_stub


def _restic_restore_fill_by_spec(commands, subtree_contents, full_tree):
    """Stub `restic restore` filler that mirrors real restic placement: a
    `snapshot:subtree` spec extracts that subtree's content directly at the
    restore target, while a bare snapshot spec recreates the recorded full tree
    (drive-letter path components included)."""

    def fill_staging(target: Path) -> None:
        spec = commands[-1][2] if commands else ""
        contents = subtree_contents.get(spec)
        _fill_tree(target, contents if contents is not None else full_tree)

    return fill_staging


def _record_missing_only_copy(recorder):
    """Patch _restore_missing_only so selection tests never touch real disk targets."""
    def fake_restore_missing_only(source, target, actions, created_paths):
        recorder.append((str(source), str(target), [entry.name for entry in sorted(source.iterdir())]))
        return True, 0

    return patch.object(backup_strategy, "_restore_missing_only", fake_restore_missing_only)


class PreserveRestoreServiceTests(unittest.TestCase):
    """S5: force_overwrite=false is now the preserve mode without destructive
    confirmation; force_overwrite=true keeps the replacement semantics."""

    def _service(self, **config_kwargs):
        dry_run = config_kwargs.pop("dry_run", False)
        config = RestoreConfig(target_path="/restore/target", source="somesnap", dry_run=dry_run, **config_kwargs)
        storage, strategy, container = Mock(), Mock(), Mock()
        storage.download_restore_candidate.return_value = "somesnap"
        strategy.restore.return_value = _make_result()
        for discovery in (
            "find_containers_using_runtime_volumes",
            "find_containers_using_volume",
            "get_containers_by_labels",
        ):
            getattr(container, discovery).return_value = []
        return RestoreService(storage, container, strategy, config), strategy

    def test_unforced_restore_runs_in_preserve_mode_without_force_flag(self):
        service, strategy = self._service(force_overwrite=False)

        result = service.execute_restore()

        self.assertTrue(result.success, result.error)
        strategy.restore.assert_called_once()
        self.assertFalse(result.force_overwrite)
        joined = "\n".join(result.planned_actions or [])
        self.assertIn("Preserve existing target contents", joined)
        self.assertNotIn("Replace target contents before restore", joined)

    def test_forced_restore_keeps_replacement_plan_text_and_flag(self):
        service, _strategy = self._service(force_overwrite=True)

        result = service.execute_restore()

        self.assertTrue(result.success, result.error)
        self.assertTrue(result.force_overwrite)
        joined = "\n".join(result.planned_actions or [])
        self.assertIn("Replace target contents before restore", joined)

    def test_dry_run_plan_reports_preserve_without_running_restore(self):
        service, strategy = self._service(dry_run=True, force_overwrite=False)

        result = service.execute_restore()

        self.assertTrue(result.success, result.error)
        strategy.restore.assert_not_called()
        self.assertTrue(result.dry_run)
        self.assertIn("Preserve existing target contents", "\n".join(result.planned_actions or []))


class RestorePreserveDirectLayoutTests(unittest.TestCase):
    """S5: direct layout preserve restores only missing items."""

    def _config(self, target, **kwargs):
        defaults = dict(
            target_path=str(target),
            source="snap",
            dry_run=False,
            force_overwrite=False,
            backup_strategy="restic",
            restic_repository="local:/repo",
            restic_password="probe",
        )
        defaults.update(kwargs)
        return RestoreConfig(**defaults)

    def with_stub_run(self, **stub_kwargs):
        return use_restic_stub(**stub_kwargs)

    def test_direct_preserve_restores_only_missing_items_and_keeps_metadata(self):
        workspace = _fresh_dir("restore-preserve-direct-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "target"
        _fill_tree(target, {"kept.txt": "target-kept", "extra.txt": "target-extra"})
        kept_mtime = 1_500_000_000
        os.utime(target / "kept.txt", (kept_mtime, kept_mtime))
        created_mtime = 1_600_000_000

        def fill_staging(staging: Path) -> None:
            _fill_tree(
                staging,
                {
                    "kept.txt": "staging-kept-changed",
                    "missing.txt": "staging-missing",
                    "nested/introduced.txt": "staging-nested",
                },
            )
            os.utime(staging / "missing.txt", (created_mtime, created_mtime))
            os.utime(staging / "nested", (created_mtime, created_mtime))

        created_paths_seen = []
        real_normalize = backup_strategy.normalize_ownership

        def recording_normalize(config, policy, created_paths=None):
            outcome = real_normalize(config, policy, created_paths=created_paths)
            created_paths_seen.append(list(created_paths or []))
            return outcome

        with self.with_stub_run(fill_staging=fill_staging), patch.object(
            backup_strategy, "normalize_ownership", recording_normalize
        ):
            result = ResticBackupStrategy().restore("snap", self._config(target))

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.destructive_state, "none")
        self.assertFalse(result.force_overwrite)
        self.assertFalse(result.partial)
        # Existing entries: exact untouched bytes and untouched metadata.
        self.assertEqual((target / "kept.txt").read_text(encoding="utf-8"), "target-kept")
        self.assertEqual(os.stat(target / "kept.txt").st_mtime, kept_mtime)
        self.assertEqual((target / "extra.txt").read_text(encoding="utf-8"), "target-extra")
        # Created entries carry the source's timestamps through copystat.
        self.assertEqual((target / "missing.txt").read_text(encoding="utf-8"), "staging-missing")
        self.assertEqual(os.stat(target / "missing.txt").st_mtime, created_mtime)
        self.assertEqual((target / "nested" / "introduced.txt").read_text(encoding="utf-8"), "staging-nested")
        # Ownership bookkeeping records only the topmost created roots; existing
        # entries are never recorded.
        self.assertEqual(
            [sorted(str(path) for path in created) for created in created_paths_seen],
            [[str(target / "missing.txt"), str(target / "nested")]],
        )

    def test_direct_preserve_keeps_file_directory_collisions_and_existing_symlinks(self):
        workspace = _fresh_dir("restore-preserve-collision-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "target"
        _fill_tree(target, {"file_at_name.txt": "target-file"})
        (target / "dir_at_name").mkdir()
        (target / "dir_at_name" / "inner.txt").write_text("target dir inner marker", encoding="utf-8")
        symlink = target / "linked.txt"
        try:
            os.symlink("kept.txt", symlink)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable in this environment")

        def fill_staging(staging: Path) -> None:
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "file_at_name.txt").write_text("staging-file", encoding="utf-8")
            (staging / "dir_at_name").mkdir()
            (staging / "dir_at_name" / "inner.txt").write_text("staging dir inner marker", encoding="utf-8")
            try:
                os.symlink("elsewhere", staging / "linked.txt")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable in this environment")

        with self.with_stub_run(fill_staging=fill_staging):
            result = ResticBackupStrategy().restore("snap", self._config(target))

        self.assertTrue(result.success, result.error)
        self.assertEqual((target / "file_at_name.txt").read_text(encoding="utf-8"), "target-file")
        self.assertEqual((target / "dir_at_name" / "inner.txt").read_text(encoding="utf-8"), "target dir inner marker")
        self.assertEqual(os.readlink(symlink), "kept.txt")

    def test_direct_preserve_rejects_read_only_target_before_any_write(self):
        workspace = _fresh_dir("restore-preserve-readonly-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "target"
        config = self._config(target, read_only_paths=(str(target),))

        with self.with_stub_run() as (run, _popen):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertFalse(result.success)
        self.assertEqual(result.category, "readonly_target")
        run.assert_not_called()

    def test_racing_file_creation_preserves_competitor_bytes_and_bookkeeping(self):
        """Deterministic check/create race: a file that appears between the
        existence check and the exclusive creation keeps the competitor's exact
        bytes (sentinel) instead of being overwritten; only genuinely created
        entries are recorded for ownership normalization."""
        workspace = _fresh_dir("restore-preserve-race-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "target"
        _fill_tree(target, {"kept.txt": "target-kept-bytes"})

        def fill_staging(staging: Path) -> None:
            _fill_tree(
                staging,
                {"kept.txt": "staging-kept-changed", "raced.txt": "staging-raced", "plain.txt": "staging-plain"},
            )

        raced_path = target / "raced.txt"
        sentinel = b"competitor-sentinel-bytes"
        original_open = os.open
        raced_fired = []

        def racing_open(path, flags, mode=0o777, **kwargs):
            if str(path).endswith("raced.txt") and (flags & getattr(os, "O_EXCL", 0)) and not raced_fired:
                raced_fired.append(True)
                # The competitor wins the race just before our exclusive creation.
                competitor = original_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
                try:
                    os.write(competitor, sentinel)
                finally:
                    os.close(competitor)
            return original_open(path, flags, mode, **kwargs)

        bookkeeping = []
        real_normalize = backup_strategy.normalize_ownership

        def recording_normalize(config, policy, created_paths=None):
            outcome = real_normalize(config, policy, created_paths=created_paths)
            bookkeeping.append([str(path) for path in (created_paths or [])])
            return outcome

        with self.with_stub_run(fill_staging=fill_staging), patch("os.open", racing_open), patch.object(
            backup_strategy, "normalize_ownership", recording_normalize
        ):
            result = ResticBackupStrategy().restore("snap", self._config(target))

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.destructive_state, "none")
        self.assertTrue(len(raced_fired) == 1, "the racing hook must have fired exactly once")
        self.assertEqual(raced_path.read_bytes(), sentinel, "the race loser's staging bytes must not overwrite")
        self.assertEqual((target / "kept.txt").read_text(encoding="utf-8"), "target-kept-bytes")
        self.assertEqual((target / "plain.txt").read_text(encoding="utf-8"), "staging-plain")
        self.assertEqual(bookkeeping, [[str(target / "plain.txt")]])

    def test_preserve_rejects_symlink_target_before_any_write(self):
        """A preserve restore through a symlink target must fail before staging
        any target write; the real backing directory keeps its exact bytes.
        Symlinks are simulated through lstat when creation is not permitted."""
        workspace = _fresh_dir("restore-preserve-symlink-target-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        real_dir = workspace / "real"
        _fill_tree(real_dir, {"inside.txt": "untouched-bytes"})
        link = workspace / "link"
        link_patcher = None
        try:
            os.symlink(str(real_dir), link)
        except (OSError, NotImplementedError):
            original_lexists = os.path.lexists
            original_lstat = os.lstat

            def fake_lexists(path):
                return (
                    os.path.abspath(str(path)).casefold() == os.path.abspath(str(link)).casefold()
                    or str(path) == str(link)
                ) or original_lexists(path)

            def fake_lstat(path):
                if os.path.abspath(str(path)).casefold() == os.path.abspath(str(link)).casefold():
                    return os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 0, 0, 0, 0, 0, 0, 0))
                return original_lstat(path)

            link_patcher = (
                patch("os.path.lexists", fake_lexists),
                patch("os.lstat", fake_lstat),
            )
            link_patcher[0].start()
            link_patcher[1].start()
            self.addCleanup(link_patcher[0].stop)
            self.addCleanup(link_patcher[1].stop)

        def fill_staging(staging: Path) -> None:
            _fill_tree(staging, {"missing.txt": "staging-content"})

        with self.with_stub_run(fill_staging=fill_staging) as (run, _popen):
            result = ResticBackupStrategy().restore("snap", self._config(str(link)))

        self.assertFalse(result.success)
        self.assertIn("through a symlink", result.error)
        self.assertEqual(run.call_args_list, [], "no staging may be built after a symlink target rejection")
        self.assertEqual((real_dir / "inside.txt").read_text(encoding="utf-8"), "untouched-bytes")
        self.assertEqual(sorted(item.name for item in real_dir.iterdir()), ["inside.txt"])

    def test_preserve_rejects_filesystem_root_target_before_any_write(self):
        config = self._config("/", force_overwrite=False)

        with self.with_stub_run() as (run, popen):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertFalse(result.success)
        self.assertIn("root", result.error)
        self.assertEqual(run.call_args_list, [])
        self.assertEqual(popen.call_args_list, [])

    def test_direct_replacement_keeps_previous_clear_and_direct_target_behavior(self):
        workspace = _fresh_dir("restore-replace-direct-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "target"
        _fill_tree(target, {"old.txt": "target-old"})
        config = self._config(target, force_overwrite=True, runtime_type="docker")

        recorded = []
        with self.with_stub_run(restore_calls=recorded):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertTrue(result.success, result.error)
        self.assertEqual(recorded[0][2], "snap")
        self.assertEqual(recorded[0][4], str(target))
        self.assertFalse((target / "old.txt").exists())
        self.assertEqual(result.destructive_state, "complete")
        self.assertTrue(result.force_overwrite)


class RestorePreserveBackupDirLayoutTests(unittest.TestCase):
    """S5: backup-dir layout preserve keeps existing (and read-only) entries."""

    def _config(self, target, **kwargs):
        defaults = dict(
            target_path=str(target),
            source="snap",
            dry_run=False,
            force_overwrite=False,
            layout="backup-dir",
            backup_strategy="restic",
            restic_repository="local:/repo",
            restic_password="probe",
        )
        defaults.update(kwargs)
        return RestoreConfig(**defaults)

    def test_backup_dir_preserve_restores_missing_entries_without_clearing(self):
        workspace = _fresh_dir("restore-preserve-backupdir-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "backup"
        existing = target / "mounted_dir"
        _fill_tree(existing, {"kept.txt": "kept-entry-content"})
        readonly = target / "readonly_data"
        _fill_tree(readonly, {"guard.txt": "readonly-content"})
        config = self._config(target, read_only_paths=(str(readonly),))

        def fill_staging(staging: Path) -> None:
            _fill_tree(staging, {"backup/mounted_dir/missing.txt": "staged-missing"})

        with use_restic_stub(fill_staging=fill_staging):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.destructive_state, "none")
        self.assertEqual((existing / "kept.txt").read_text(encoding="utf-8"), "kept-entry-content")
        self.assertEqual((existing / "missing.txt").read_text(encoding="utf-8"), "staged-missing")
        self.assertEqual((readonly / "guard.txt").read_text(encoding="utf-8"), "readonly-content")
        joined = "\n".join(result.planned_actions or [])
        self.assertIn("Read-only mount skipped intentionally", joined)


class OriginalPathSubtreeSelectionTests(unittest.TestCase):
    """S4: target exactly matching one configured native source restores only
    that source's snapshot subtree; wrong or unsupported subtrees fail closed
    before any mutation. Disk writes are limited to temp staging areas; the
    no-nesting end-to-end proof lives in the real-Restic integration tests."""

    def _config(self, target, sources, **kwargs):
        defaults = dict(
            target_path=str(target),
            source="snap",
            dry_run=False,
            force_overwrite=False,
            backup_strategy="restic",
            runtime_type="native",
            filesystem_paths=tuple(sources),
            restic_repository="local:/repo",
            restic_password="probe",
        )
        defaults.update(kwargs)
        return RestoreConfig(**defaults)

    def _drive_dir_subtree_nodes(self):
        return {"/C/Data/App": [{"name": "App", "type": "dir", "path": "/C/Data/App"}]}

    def test_exact_source_target_selects_only_that_snapshot_subtree(self):
        config = self._config("C:\\Data\\App", ["C:\\Data\\App", "D:\\Other"])
        recorder = []
        fill_ls_result = self._drive_dir_subtree_nodes()
        fill_ls_result.update({"/D/Other": [{"name": "Other", "type": "dir", "path": "/D/Other"}]})
        with use_restic_stub(nodes_by_filter=fill_ls_result), _record_missing_only_copy(recorder):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertTrue(result.success, result.error)
        copied = recorder[0]
        self.assertEqual(copied[1].replace("\\", "/"), "C:/Data/App")
        # The selected single source was restored; the sibling source was never listed, staged, or copied.
        self.assertFalse(any("D:" in copy[0] or "D:" in copy[1] for copy in recorder))

    def test_windows_drive_source_converts_to_snapshot_subtree(self):
        config = self._config("c:\\data\\app", ["C:\\Data\\App"])
        recorder = []
        with use_restic_stub(nodes_by_filter=self._drive_dir_subtree_nodes()) as (run, popen), _record_missing_only_copy(recorder):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertTrue(result.success, result.error)
        ls_command = next(command for command in popen.call_args_list if command.args[0][1] == "ls")
        self.assertIn("/C/Data/App", ls_command.args[0])
        restore_command = next(command for command in run.call_args_list if command.args[0][1] == "restore")
        self.assertNotIn("--overwrite", restore_command.args[0])
        self.assertEqual(restore_command.args[0][2], "snap:/C/Data/App")

    def test_posix_source_target_selects_posix_subtree(self):
        config = self._config("/srv/app", ["/srv/app"])
        recorder = []
        with use_restic_stub(
            nodes_by_filter={"/srv/app": [{"name": "app", "type": "dir", "path": "/srv/app"}]}
        ) as (run, _popen), _record_missing_only_copy(recorder):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertTrue(result.success, result.error)
        restore_command = next(command for command in run.call_args_list if command.args[0][1] == "restore")
        self.assertEqual(restore_command.args[0][2], "snap:/srv/app")

    def test_case_insensitive_windows_selection_matches_configured_source_case(self):
        config = self._config("c:\\data\\app", [r"C:\\Data\\App"])
        recorder = []
        with use_restic_stub(nodes_by_filter=self._drive_dir_subtree_nodes()) as (_run, _popen), _record_missing_only_copy(recorder):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertTrue(result.success, result.error)

    def test_docker_runtime_with_matching_target_keeps_full_tree_layout(self):
        """A Docker config that happens to carry filesystem_paths must keep the
        previous full-tree behavior even when its target equals a source."""
        workspace = _fresh_dir("restore-docker-fulltree-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "target"
        config = self._config(target, [str(target)], runtime_type="docker")

        def fill_staging(staging: Path) -> None:
            _fill_tree(staging, {"missing.txt": "full-tree-content"})

        with use_restic_stub(fill_staging=fill_staging) as (run, popen):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertTrue(result.success, result.error)
        restore_command = next(command for command in run.call_args_list if command.args[0][1] == "restore")
        self.assertEqual(restore_command.args[0][2], "snap", "no snapshot:subtree spec outside native runtime")
        self.assertEqual(popen.call_args_list, [], "no subtree listing outside native runtime")
        self.assertEqual((target / "missing.txt").read_text(encoding="utf-8"), "full-tree-content")

    def test_missing_snapshot_subtree_fails_closed_before_mutation_in_replacement_layout(self):
        """The real configured target keeps its bytes: the clear is never invoked."""
        workspace = _fresh_dir("restore-subtree-missing-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "target"
        _fill_tree(target, {"guard.txt": "pre-existing-guard"})
        config = self._config(target, [str(target)], force_overwrite=True)
        cleared = []

        with use_restic_stub(nodes_by_filter={}), patch.object(
            backup_strategy, "_clear_target_contents", lambda target_path: cleared.append(str(target_path))
        ):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertFalse(result.success)
        self.assertIn("does not contain", result.error)
        self.assertIn("configured source", result.error)
        self.assertEqual(cleared, [], "preflight failure must never clear the target")
        self.assertEqual((target / "guard.txt").read_text(encoding="utf-8"), "pre-existing-guard")

    def test_preflight_rejects_child_only_listing_without_accepting_first_node(self):
        """A filtered listing that only returns a child entry must not be accepted:
        only a node whose recorded path equals the subtree validates it, so the
        clear is never invoked and the real configured target keeps its bytes."""
        workspace = _fresh_dir("restore-subtree-child-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "target"
        _fill_tree(target, {"guard.txt": "pre-existing-guard"})
        config = self._config(target, [str(target)], force_overwrite=True)
        derived = backup_strategy._native_snapshot_subtree(str(target))
        nodes = {derived: [{"name": "child.txt", "type": "file", "path": derived + "/child.txt"}]}
        cleared = []

        with use_restic_stub(nodes_by_filter=nodes), patch.object(
            backup_strategy, "_clear_target_contents", lambda target_path: cleared.append(str(target_path))
        ):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertFalse(result.success)
        self.assertIn("does not contain", result.error)
        self.assertEqual(cleared, [])
        self.assertEqual((target / "guard.txt").read_text(encoding="utf-8"), "pre-existing-guard")

    def test_preflight_spawn_failure_never_clears_target(self):
        with tempfile.TemporaryDirectory(prefix="restore-spawn-guard-") as directory:
            target = Path(directory) / "source"
            _fill_tree(target, {"guard.txt": "untouched"})
            config = self._config(str(target), [str(target)], force_overwrite=True)
            with patch.object(backup_strategy, "_require_executable", return_value="restic"), patch.object(
                backup_strategy.subprocess, "Popen", side_effect=OSError("cannot spawn restic")
            ), patch.object(backup_strategy, "_clear_target_contents") as clear, patch.object(
                backup_strategy.subprocess, "run"
            ) as run:
                result = ResticBackupStrategy().restore("snap", config)
            self.assertFalse(result.success)
            self.assertEqual(result.category, "native_subtree_missing")
            self.assertIn("cannot spawn restic", result.error)
            clear.assert_not_called()
            run.assert_not_called()
            self.assertEqual((target / "guard.txt").read_text(encoding="utf-8"), "untouched")

    def test_preflight_fails_closed_on_malformed_ls_output(self):
        workspace = _fresh_dir("restore-subtree-malformed-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "target"
        _fill_tree(target, {"guard.txt": "pre-existing-guard"})
        config = self._config(target, [str(target)], force_overwrite=True)
        cleared = []

        with use_restic_stub(ls_payload_by_filter={"ignored": b"not-json-at-all\n"}), patch.object(
            backup_strategy, "_clear_target_contents", lambda target_path: cleared.append(str(target_path))
        ):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertFalse(result.success)
        self.assertIn("non-JSON", result.error)
        self.assertEqual(cleared, [])
        self.assertEqual((target / "guard.txt").read_text(encoding="utf-8"), "pre-existing-guard")

    def test_preflight_fails_closed_on_oversized_ls_output(self):
        workspace = _fresh_dir("restore-subtree-oversized-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "target"
        _fill_tree(target, {"guard.txt": "pre-existing-guard"})
        config = self._config(target, [str(target)], force_overwrite=True)
        cleared = []

        with use_restic_stub(ls_payload_by_filter={"ignored": b"x" * 4096 + b"\n"}), patch.object(
            backup_strategy, "_clear_target_contents", lambda target_path: cleared.append(str(target_path))
        ), patch.object(ResticBackupStrategy, "_LS_PREFLIGHT_BUDGET_BYTES", 16):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertFalse(result.success)
        self.assertIn("bounded verification budget", result.error)
        self.assertEqual(cleared, [])
        self.assertEqual((target / "guard.txt").read_text(encoding="utf-8"), "pre-existing-guard")

    def test_file_source_subtree_is_rejected_before_mutation(self):
        workspace = _fresh_dir("restore-subtree-file-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        source_path = workspace / "single.db"
        source_path.write_text("real-file-bytes", encoding="utf-8")
        config = self._config(source_path, [str(source_path)], force_overwrite=True)
        derived = backup_strategy._native_snapshot_subtree(str(source_path))
        nodes = {derived: [{"name": "single.db", "type": "file", "path": derived}]}
        cleared = []

        with use_restic_stub(nodes_by_filter=nodes), patch.object(
            backup_strategy, "_clear_target_contents", lambda target_path: cleared.append(str(target_path))
        ):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertFalse(result.success)
        error = result.error.lower()
        self.assertIn("file", error)
        self.assertIn("single.db", error)
        self.assertIn("configured native source", result.error)
        self.assertEqual(cleared, [], "file-source rejection must precede any destructive action")
        self.assertEqual(source_path.read_text(encoding="utf-8"), "real-file-bytes")

    def test_unc_original_path_fails_safely_before_mutation(self):
        workspace = _fresh_dir("restore-unc-guard-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "target"
        _fill_tree(target, {"guard.txt": "pre-existing-guard"})
        config = self._config(r"\\\\server\\share\\app", [r"\\\\server\\share\\app"])

        with use_restic_stub() as (run, _popen), _record_missing_only_copy([]):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertFalse(result.success)
        self.assertIn("UNC", result.error)
        self.assertIn("not supported", result.error)
        self.assertEqual(run.call_args_list, [], "no restic command may run for an unsupported original path")
        self.assertEqual((target / "guard.txt").read_text(encoding="utf-8"), "pre-existing-guard")

    def test_replacement_mode_with_explicit_subtree_clears_only_after_subtree_validation(self):
        workspace = _fresh_dir("restore-subtree-replace-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        disk_target = workspace / "target"
        _fill_tree(disk_target, {"old.txt": "target-old"})
        cleared = []
        config = self._config("C:\\Data\\App", ["C:\\Data\\App"], force_overwrite=True)

        def recorded_clear(target_path):
            cleared.append(str(target_path))

        with use_restic_stub(nodes_by_filter=self._drive_dir_subtree_nodes()), patch.object(
            backup_strategy, "_clear_target_contents", recorded_clear
        ), patch.object(backup_strategy, "_restore_missing_only", Mock()):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertTrue(result.success, result.error)
        self.assertEqual(cleared, ["C:\\Data\\App"])
        self.assertEqual(result.destructive_state, "complete")


@unittest.skipUnless(shutil.which("restic"), "restic executable is unavailable")
class RealResticRestorePreserveIntegrationTests(unittest.TestCase):
    """Genuine public-interface restore against a local synthetic restic repo."""


class NativeCustomDestinationPlacementTests(unittest.TestCase):
    """S6: a native custom destination that is not a filesystem root receives
    the content of EVERY configured source directly at the destination root
    (like the docker mounted-entries flow, placed inside the destination); no
    recorded path component (drive letter included) is recreated under it. Only
    filesystem-root destinations (POSIX root, Windows drive root, UNC share
    root) keep the recorded full-tree layout, and a native restore without
    configured sources fails closed before any mutation."""

    def _config(self, target, sources, **kwargs):
        defaults = dict(
            target_path=str(target),
            source="snap",
            dry_run=False,
            force_overwrite=False,
            backup_strategy="restic",
            runtime_type="native",
            filesystem_paths=tuple(sources),
            restic_repository="local:/repo",
            restic_password="probe",
        )
        defaults.update(kwargs)
        return RestoreConfig(**defaults)

    @staticmethod
    def _restore_commands(run_stub):
        return [call.args[0] for call in run_stub.call_args_list if call.args[0][1] == "restore"]

    @staticmethod
    def _ls_subtrees(popen_stub):
        return [call.args[0][-1] for call in popen_stub.call_args_list if call.args[0][1] == "ls"]

    def test_custom_destination_extracts_source_content_directly_without_path_nesting(self):
        workspace = _fresh_dir("restore-s6-placement-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "destination"
        _fill_tree(target, {"user-note.txt": "user"})
        config = self._config(target, [r"L:\\prueba_nativa"])
        subtree_contents = {"snap:/L/prueba_nativa": {"app.txt": "native-app", "nested/deep.txt": "deep"}}
        full_tree = {"L/prueba_nativa/app.txt": "native-app", "L/prueba_nativa/nested/deep.txt": "deep"}
        commands = []

        with use_restic_stub(
            nodes_by_filter={"/L/prueba_nativa": [{"name": "prueba_nativa", "type": "dir", "path": "/L/prueba_nativa"}]},
            fill_staging=_restic_restore_fill_by_spec(commands, subtree_contents, full_tree),
            restore_calls=commands,
        ) as (run_stub, popen_stub):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertTrue(result.success, result.error)
        self.assertEqual([command[2] for command in self._restore_commands(run_stub)], ["snap:/L/prueba_nativa"])
        self.assertEqual(self._ls_subtrees(popen_stub), ["/L/prueba_nativa"])
        self.assertEqual((target / "app.txt").read_text(encoding="utf-8"), "native-app")
        self.assertEqual((target / "nested" / "deep.txt").read_text(encoding="utf-8"), "deep")
        self.assertEqual((target / "user-note.txt").read_text(encoding="utf-8"), "user")
        self.assertEqual(
            sorted(item.name for item in target.iterdir()),
            ["app.txt", "nested", "user-note.txt"],
            "no drive-letter nor recorded path component may be recreated",
        )
        self.assertIn("directly", "\n".join(result.planned_actions or []))

    def test_multi_source_custom_destination_places_every_source_at_root_and_keeps_existing_files(self):
        workspace = _fresh_dir("restore-s6-multi-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "destination"
        _fill_tree(target, {"kept.txt": "target-kept"})
        config = self._config(target, [r"L:\\prueba_nativa", r"D:\\Other"])
        subtree_contents = {
            "snap:/L/prueba_nativa": {"app.txt": "native-app"},
            "snap:/D/Other": {"other.txt": "other-app"},
        }
        full_tree = {"L/prueba_nativa/app.txt": "native-app", "D/Other/other.txt": "other-app"}
        commands = []

        with use_restic_stub(
            nodes_by_filter={
                "/L/prueba_nativa": [{"name": "prueba_nativa", "type": "dir", "path": "/L/prueba_nativa"}],
                "/D/Other": [{"name": "Other", "type": "dir", "path": "/D/Other"}],
            },
            fill_staging=_restic_restore_fill_by_spec(commands, subtree_contents, full_tree),
            restore_calls=commands,
        ) as (run_stub, popen_stub):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertTrue(result.success, result.error)
        self.assertEqual(self._ls_subtrees(popen_stub), ["/L/prueba_nativa", "/D/Other"])
        self.assertEqual(
            [command[2] for command in self._restore_commands(run_stub)],
            ["snap:/L/prueba_nativa", "snap:/D/Other"],
        )
        self.assertEqual((target / "app.txt").read_text(encoding="utf-8"), "native-app")
        self.assertEqual((target / "other.txt").read_text(encoding="utf-8"), "other-app")
        self.assertEqual((target / "kept.txt").read_text(encoding="utf-8"), "target-kept")
        self.assertEqual(sorted(item.name for item in target.iterdir()), ["app.txt", "kept.txt", "other.txt"])
        self.assertEqual(result.destructive_state, "none")

    def test_preflight_validates_every_source_before_any_mutation(self):
        workspace = _fresh_dir("restore-s6-preflight-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "destination"
        _fill_tree(target, {"guard.txt": "pre-existing-guard"})
        config = self._config(target, [r"L:\\prueba_nativa", r"D:\\Ghost"], force_overwrite=True)
        cleared = []
        commands = []

        with use_restic_stub(
            nodes_by_filter={"/L/prueba_nativa": [{"name": "prueba_nativa", "type": "dir", "path": "/L/prueba_nativa"}]},
            fill_staging=_restic_restore_fill_by_spec(commands, {}, {}),
            restore_calls=commands,
        ) as (run_stub, popen_stub), patch.object(
            backup_strategy, "_clear_target_contents", lambda target_path: cleared.append(str(target_path))
        ):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertFalse(result.success)
        self.assertIn("does not contain", result.error)
        self.assertEqual(self._ls_subtrees(popen_stub), ["/L/prueba_nativa", "/D/Ghost"])
        self.assertEqual(cleared, [], "preflight failure must precede the replace-mode clear")
        self.assertEqual(self._restore_commands(run_stub), [], "no restore may run after a failed preflight")
        self.assertEqual((target / "guard.txt").read_text(encoding="utf-8"), "pre-existing-guard")

    def test_replace_mode_custom_destination_clears_once_and_restores_each_subtree_into_target(self):
        workspace = _fresh_dir("restore-s6-replace-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "destination"
        config = self._config(target, [r"L:\\prueba_nativa", r"D:\\Other"], force_overwrite=True)
        subtree_contents = {
            "snap:/L/prueba_nativa": {"app.txt": "native-app"},
            "snap:/D/Other": {"other.txt": "other-app"},
        }
        full_tree = {"L/prueba_nativa/app.txt": "native-app", "D/Other/other.txt": "other-app"}
        commands = []
        cleared = []

        with use_restic_stub(
            nodes_by_filter={
                "/L/prueba_nativa": [{"name": "prueba_nativa", "type": "dir", "path": "/L/prueba_nativa"}],
                "/D/Other": [{"name": "Other", "type": "dir", "path": "/D/Other"}],
            },
            fill_staging=_restic_restore_fill_by_spec(commands, subtree_contents, full_tree),
            restore_calls=commands,
        ) as (run_stub, popen_stub), patch.object(
            backup_strategy, "_clear_target_contents", lambda target_path: cleared.append(str(target_path))
        ):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertTrue(result.success, result.error)
        self.assertEqual(cleared, [str(config.target_path)], "the target is cleared exactly once before every subtree restore")
        restore_commands = self._restore_commands(run_stub)
        self.assertEqual([command[2] for command in restore_commands], ["snap:/L/prueba_nativa", "snap:/D/Other"])
        for command in restore_commands:
            self.assertEqual(command[command.index("--target") + 1], str(config.target_path))
        self.assertEqual((target / "app.txt").read_text(encoding="utf-8"), "native-app")
        self.assertEqual((target / "other.txt").read_text(encoding="utf-8"), "other-app")
        self.assertEqual(sorted(item.name for item in target.iterdir()), ["app.txt", "other.txt"])
        self.assertEqual(result.destructive_state, "complete")

    def test_custom_posix_and_unc_destinations_place_subtree_content_at_target_root(self):
        for destination, source, subtree in (
            ("/restore/custom", "/srv/app", "/srv/app"),
            (r"\\server\share\restore\custom", r"D:\\Data\\App", "/D/Data/App"),
        ):
            with self.subTest(destination=destination):
                config = self._config(destination, [source])
                recorder = []
                subtree_contents = {f"snap:{subtree}": {"app.txt": "native-app"}}
                full_tree = {"component/root.txt": "full-tree"}  # bare-snapshot placement never matches subtree extraction
                commands = []

                with use_restic_stub(
                    nodes_by_filter={subtree: [{"name": subtree.rsplit("/", 1)[-1], "type": "dir", "path": subtree}]},
                    fill_staging=_restic_restore_fill_by_spec(commands, subtree_contents, full_tree),
                    restore_calls=commands,
                ) as (run_stub, popen_stub), _record_missing_only_copy(recorder):
                    result = ResticBackupStrategy().restore("snap", config)

                self.assertTrue(result.success, result.error)
                self.assertEqual([command[2] for command in self._restore_commands(run_stub)], [f"snap:{subtree}"])
                self.assertEqual(self._ls_subtrees(popen_stub), [subtree])
                # The staged content starts at the destination root directly: the
                # recorder sees the subtree's entries, never a component directory.
                self.assertEqual(recorder[0][2], ["app.txt"])
                # Compare with collapsed separators: Windows normalizes them, POSIX
                # hosts (CI) keep backslashes literally in the recorded path.
                recorded = re.sub(r"/+", "/", str(recorder[0][1]).replace("\\", "/"))
                self.assertTrue(recorded.endswith("restore/custom"), str(recorder[0][1]))

    def test_filesystem_root_destinations_keep_full_tree_layout(self):
        for destination, full_tree in (
            (r"L:\\", {"L/prueba_nativa/app.txt": "native-app", "L/prueba_nativa/docs/beta.txt": "beta"}),
            ("/", {"srv/app/app.txt": "native-app"}),
            (r"\\server\\share", {"server/share/prueba_nativa/app.txt": "native-app"}),
        ):
            with self.subTest(destination=destination):
                config = self._config(destination, [r"L:\\prueba_nativa"], force_overwrite=True)
                commands = []
                cleared = []
                placed = []

                def fill_staging(recorded_target: Path, full_tree=full_tree):
                    # Root destinations point at real filesystem roots; the stub
                    # records the placement instead of writing there.
                    placed.append(sorted(full_tree))

                with use_restic_stub(fill_staging=fill_staging, restore_calls=commands) as (run_stub, popen_stub), patch.object(
                    backup_strategy, "_clear_target_contents", lambda target_path: cleared.append(str(target_path))
                ), patch.object(backup_strategy, "_sqlite_warnings", return_value=[]):
                    result = ResticBackupStrategy().restore("snap", config)

                self.assertTrue(result.success, result.error)
                restore_commands = self._restore_commands(run_stub)
                self.assertEqual([command[2] for command in restore_commands], ["snap"], "the recorded full-tree layout stays as-is")
                for command in restore_commands:
                    self.assertEqual(command[command.index("--target") + 1], str(config.target_path))
                self.assertEqual(self._ls_subtrees(popen_stub), [], "root destinations need no subtree validation")
                self.assertEqual(placed, [sorted(full_tree)])
                self.assertEqual(cleared, [str(config.target_path)])
                self.assertEqual(result.destructive_state, "complete")

    def test_native_restore_without_configured_sources_fails_before_mutation(self):
        workspace = _fresh_dir("restore-s6-no-sources-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        target = workspace / "destination"
        _fill_tree(target, {"guard.txt": "pre-existing-guard"})
        config = self._config(target, [], force_overwrite=True)
        cleared = []

        with use_restic_stub() as (run_stub, popen_stub), patch.object(
            backup_strategy, "_clear_target_contents", lambda target_path: cleared.append(str(target_path))
        ):
            result = ResticBackupStrategy().restore("snap", config)

        self.assertFalse(result.success)
        self.assertEqual(result.category, "native_sources_required")
        self.assertIn("configured sources", result.error)
        self.assertEqual(cleared, [])
        self.assertEqual(self._restore_commands(run_stub), [])
        self.assertEqual(self._ls_subtrees(popen_stub), [])
        self.assertEqual((target / "guard.txt").read_text(encoding="utf-8"), "pre-existing-guard")

@unittest.skipUnless(shutil.which("restic"), "restic executable is unavailable")
class RealResticRestorePreserveIntegrationTests(unittest.TestCase):
    """Genuine public-interface restore against a local synthetic restic repo."""

    _BOUNDED = 300

    @classmethod
    def setUpClass(cls):
        cls.workspace = Path(tempfile.mkdtemp(prefix="restore-preserve-integration-"))
        cls.repo = cls.workspace / "repo"
        cls.src = cls.workspace / "src"
        _fill_tree(cls.src, {"alpha.txt": "alpha-v1", "gone.txt": "gone", "docs/beta.txt": "beta"})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workspace, ignore_errors=True)

    def setUp(self):
        self._reset_source()
        repository = f"local:{self.repo.resolve()}"
        self._env_patcher = patch.dict(
            os.environ,
            {"RESTIC_REPOSITORY": repository, "RESTIC_PASSWORD": "probe"},
        )
        self._env_patcher.start()
        self._real_run = backup_strategy.subprocess.run

        def bounded_run(command, **kwargs):
            kwargs.setdefault("timeout", self._BOUNDED)
            return self._real_run(command, **kwargs)

        self._run_patcher = patch.object(backup_strategy.subprocess, "run", bounded_run)
        self._run_patcher.start()

    def tearDown(self):
        self._run_patcher.stop()
        self._env_patcher.stop()

    def _reset_source(self):
        """Each test backs up and restores through the same class workspace; the
        source directory must start pristine so snapshots are deterministic."""
        for child in self.src.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        _fill_tree(self.src, {"alpha.txt": "alpha-v1", "gone.txt": "gone", "docs/beta.txt": "beta"})

    def _init_and_backup(self, extra_paths=None):
        env = dict(
            os.environ,
            RESTIC_REPOSITORY=f"local:{self.repo.resolve()}",
            RESTIC_PASSWORD="probe",
        )
        init = subprocess.run(
            ["restic", "init"], env=env, capture_output=True, timeout=self._BOUNDED, check=False, text=True
        )
        combined = (init.stderr or "") + (init.stdout or "")
        if init.returncode != 0 and "already" not in combined.lower():
            init.check_returncode()
        self._backup_paths([str(self.src)], env=env)

    def _backup_paths(self, paths, env=None):
        env = env or dict(
            os.environ,
            RESTIC_REPOSITORY=f"local:{self.repo.resolve()}",
            RESTIC_PASSWORD="probe",
        )
        config = SimpleNamespace(
            source_paths=paths,
            restic_repository=env["RESTIC_REPOSITORY"],
            restic_password="probe",
            restic_keep_last=3,
            restic_keep_hourly=None,
            restic_keep_daily=None,
            restic_keep_weekly=None,
            restic_keep_monthly=None,
            restic_keep_yearly=None,
            restic_prune=False,
        )
        backup = ResticBackupStrategy().perform_backup(config)
        self.assertTrue(backup.success)

    def _service_config(self, force_overwrite):
        return RestoreConfig(
            target_path=str(self.src),
            dry_run=False,
            force_overwrite=force_overwrite,
            stop_containers=False,
            backup_strategy="restic",
            runtime_type="native",
            filesystem_paths=(str(self.src),),
            restic_repository=f"local:{self.repo.resolve()}",
            restic_password="probe",
        )

    def _service(self, config):
        storage, container = Mock(), Mock()
        storage.list_restore_candidates.return_value = [
            RestoreCandidate(source="latest", strategy="restic", available=True, created_at=datetime.now())
        ]
        storage.download_restore_candidate.return_value = "latest"
        return RestoreService(storage, container, ResticBackupStrategy(), config)

    def test_original_path_preserve_restores_only_missing_items_without_nesting(self):
        self._init_and_backup()
        # Mutate the original destination after the snapshot: change an existing
        # file with distinguishable metadata, delete one, add two.
        (self.src / "alpha.txt").write_text("alpha-v2", encoding="utf-8")
        (self.src / "gone.txt").unlink()
        _fill_tree(self.src, {"added-by-user.txt": "user", "extra.txt": "user-extra"})
        preserved_mtime = 1_500_000_000
        os.utime(self.src / "alpha.txt", (preserved_mtime, preserved_mtime))

        result = self._service(self._service_config(force_overwrite=False)).execute_restore()

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.destructive_state, "none")
        self.assertFalse(result.force_overwrite)
        # Existing entries (content AND metadata) are untouched.
        self.assertEqual((self.src / "alpha.txt").read_text(encoding="utf-8"), "alpha-v2")
        self.assertEqual(os.stat(self.src / "alpha.txt").st_mtime, preserved_mtime)
        self.assertEqual((self.src / "docs" / "beta.txt").read_text(encoding="utf-8"), "beta")
        self.assertEqual((self.src / "added-by-user.txt").read_text(encoding="utf-8"), "user")
        self.assertEqual((self.src / "extra.txt").read_text(encoding="utf-8"), "user-extra")
        # Only the missing snapshot file is restored, directly into the target (no nested path).
        self.assertEqual((self.src / "gone.txt").read_text(encoding="utf-8"), "gone")

    def test_original_path_replacement_restores_snapshot_exactly(self):
        self._init_and_backup()
        (self.src / "alpha.txt").write_text("alpha-v2", encoding="utf-8")
        _fill_tree(self.src, {"added-by-user.txt": "user"})

        result = self._service(self._service_config(force_overwrite=True)).execute_restore()

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.destructive_state, "complete")
        self.assertTrue(result.force_overwrite)
        names = sorted(item.name for item in self.src.iterdir())
        self.assertEqual(names, ["alpha.txt", "docs", "gone.txt"])
        self.assertEqual((self.src / "alpha.txt").read_text(encoding="utf-8"), "alpha-v1")
        self.assertEqual((self.src / "gone.txt").read_text(encoding="utf-8"), "gone")

    def test_multisource_original_path_restore_leaves_sibling_sources_untouched(self):
        self._init_and_backup()
        sibling = self.workspace / "sibling"
        _fill_tree(sibling, {"sibling.txt": "sibling-marker"})
        self._backup_paths([str(self.src), str(sibling)])
        (self.src / "alpha.txt").write_text("alpha-v2", encoding="utf-8")
        (self.src / "gone.txt").unlink()
        _fill_tree(self.src, {"added-by-user.txt": "user"})

        config = self._service_config(force_overwrite=False)
        config = RestoreConfig(
            **{
                **{field: getattr(config, field) for field in RestoreConfig.__dataclass_fields__ if field != "filesystem_paths"},
                "filesystem_paths": (str(self.src), str(sibling)),
            }
        )

        result = self._service(config).execute_restore()

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.destructive_state, "none")
        # Only the selected source is restored; the sibling snapshot subtree
        # never appears under the target, and the sibling directory is untouched.
        self.assertTrue((sibling / "sibling.txt").exists())
        self.assertEqual((sibling / "sibling.txt").read_text(encoding="utf-8"), "sibling-marker")
        self.assertFalse((self.src / "sibling.txt").exists())
        self.assertEqual((self.src / "alpha.txt").read_text(encoding="utf-8"), "alpha-v2")
        self.assertEqual((self.src / "gone.txt").read_text(encoding="utf-8"), "gone")
        self.assertEqual((self.src / "added-by-user.txt").read_text(encoding="utf-8"), "user")

    def _custom_destination_service_config(self, target: Path, force_overwrite: bool, sources: tuple[str, ...]):
        return RestoreConfig(
            target_path=str(target),
            dry_run=False,
            force_overwrite=force_overwrite,
            stop_containers=False,
            backup_strategy="restic",
            runtime_type="native",
            filesystem_paths=sources,
            restic_repository=f"local:{self.repo.resolve()}",
            restic_password="probe",
        )

    def test_custom_destination_direct_preserve_places_snapshot_content_at_destination_root(self):
        self._init_and_backup()
        destination = self.workspace / "custom-out"
        destination.mkdir()
        (destination / "user-file.txt").write_text("user", encoding="utf-8")
        (self.src / "alpha.txt").write_text("alpha-v2", encoding="utf-8")
        (self.src / "gone.txt").unlink()

        config = self._custom_destination_service_config(
            destination, force_overwrite=False, sources=(str(self.src),)
        )
        result = self._service(config).execute_restore()

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.destructive_state, "none")
        # Every snapshot item is placed directly at the destination: no recorded
        # path component (drive letter included) is recreated underneath it.
        self.assertEqual(sorted(item.name for item in destination.iterdir()), ["alpha.txt", "docs", "gone.txt", "user-file.txt"])
        self.assertEqual((destination / "alpha.txt").read_text(encoding="utf-8"), "alpha-v1")
        self.assertEqual((destination / "docs" / "beta.txt").read_text(encoding="utf-8"), "beta")
        self.assertEqual((destination / "gone.txt").read_text(encoding="utf-8"), "gone")
        self.assertEqual((destination / "user-file.txt").read_text(encoding="utf-8"), "user")

    def test_custom_destination_direct_multi_source_places_both_contents_at_destination_root(self):
        self._init_and_backup()
        sibling = self.workspace / "sibling-custom"
        _fill_tree(sibling, {"sibling.txt": "sibling-marker"})
        self._backup_paths([str(self.src), str(sibling)])
        (self.src / "gone.txt").unlink()
        destination = self.workspace / "custom-out-multi"
        destination.mkdir()
        (destination / "user-file.txt").write_text("user", encoding="utf-8")

        config = self._custom_destination_service_config(
            destination, force_overwrite=False, sources=(str(self.src), str(sibling))
        )
        result = self._service(config).execute_restore()

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.destructive_state, "none")
        self.assertEqual(
            sorted(item.name for item in destination.iterdir()),
            ["alpha.txt", "docs", "gone.txt", "sibling.txt", "user-file.txt"],
        )
        self.assertEqual((destination / "alpha.txt").read_text(encoding="utf-8"), "alpha-v1")
        self.assertEqual((destination / "sibling.txt").read_text(encoding="utf-8"), "sibling-marker")
        self.assertEqual((destination / "gone.txt").read_text(encoding="utf-8"), "gone")
        self.assertEqual((destination / "user-file.txt").read_text(encoding="utf-8"), "user")
        self.assertTrue((sibling / "sibling.txt").exists(), "the sibling source directory itself is untouched")

if __name__ == "__main__":
    unittest.main()
