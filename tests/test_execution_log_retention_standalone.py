"""Standalone isolation tests for the execution-log retention review slice.

Unlike ``tests/test_execution_log_retention.py``, this file imports ONLY the
untracked retention module and its committed delegation targets (the
committed helpers :class:`ExecutionLogPaths`, ``execution_log_posix`` and
``execution_log_windows`` are loaded through :class:`ExecutionLogRetention`
itself): the untracked capture facade is never imported, so this file
proves the retention slice is verifiable in isolation. Every fixture is
synthetic and project-local (temporary directories created and removed by
each test; no real Docker, no real host log locations).

Covers S1 (fixed managed structure is the only deletion surface), S2 (the
docker and native runtime kinds), S3 (the 30-day window and its clamping),
S4 (fail-open summary reporting) and S5 (race containment: an ancestor or
root swap never redirects a deletion outside the pinned boundary). S6 is
the reviewability of this slice itself.

No RED phase was possible for this file: the implementation already exists
and is covered by the older suite, so these tests were written to observe
the existing behavior and no intended failure was captured beforehand.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODULE_DIRECTORY = _REPO_ROOT / "src" / "worker_agent" / "infrastructure" / "security"

try:
    from src.worker_agent.infrastructure.security.execution_log_paths import ExecutionLogPaths
    from src.worker_agent.infrastructure.security.execution_log_retention import ExecutionLogRetention
except ModuleNotFoundError:  # pragma: no cover - direct execution from tests/
    sys.path.insert(0, str(_REPO_ROOT))
    from src.worker_agent.infrastructure.security.execution_log_paths import ExecutionLogPaths  # type: ignore[no-redef]
    from src.worker_agent.infrastructure.security.execution_log_retention import ExecutionLogRetention  # type: ignore[no-redef]

_POSIX = os.name != "nt"


@lru_cache(maxsize=1)
def _pinned_fd_resolves_after_rename() -> bool:
    """Whether a descriptor pinned on a directory still resolves its named
    children after the directory itself is renamed (ext4 yes; WSL ``/mnt/c``
    DrvFs no). The POSIX root-swap test asserts exact counts, so it runs
    only on a filesystem where one outcome is deterministic."""
    if os.name == "nt":
        return True  # POSIX-only behavior; Windows tests do not depend on it.
    envelope = Path(tempfile.mkdtemp(prefix="retention-standalone-fd-probe-"))
    directory = envelope / "pinned"
    (directory / "child").mkdir(parents=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.rename(directory, directory.with_name("pinned-moved"))
        try:
            os.open("child", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            return True
        except OSError:
            return False
    finally:
        os.close(fd)
        shutil.rmtree(envelope, ignore_errors=True)


class ExecutionLogRetentionStandaloneCoreTests(unittest.TestCase):
    """S1-S4: the 30-day boundary, legitimate deletion, preservation of
    unrelated files and fail-open reporting, observed through ``prune`` only
    (exit/status/side-effects and exact summary counts)."""

    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="retention-standalone-"))

    def tearDown(self):
        shutil.rmtree(self._temporary, ignore_errors=True)
        os.environ.pop("WORKER_EXECUTION_LOG_ROOT", None)
        os.environ.pop("WORKER_EXECUTION_LOG_RETENTION_DAYS", None)

    @staticmethod
    def _stale(root: Path, relative: str, mtime: float, *, kind: str = "evidence") -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(kind, encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_prune_deletes_only_stale_managed_logs_at_the_thirty_day_boundary(self):
        """Both runtime kinds (S2) on the fixed layout (S1): a log exactly
        inside the 30-day window is kept, a log just outside is deleted and
        its emptied date directory collapses; unmanaged files survive."""
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        boundary = (now - timedelta(days=30)).timestamp()
        root = self._temporary / "logs"
        stale = self._stale(root, "w-a/docker/20240401/job-old-1717200000.log", boundary - 2)
        fresh = self._stale(root, "w-b/native/20240501/job-new-1717200001.log", boundary + 2)
        wrong_name = self._stale(root, "w-a/native/20240401/notmanaged.log", boundary - 2)
        other_shape = self._stale(root, "w-a/other/20240401/job-old-1717200000.log", boundary - 2)
        stray = self._stale(root, "unrelated/job-old-1717200000.log", boundary - 2)
        summary = ExecutionLogRetention.prune(root, now=now, retention_days=30)
        self.assertEqual(summary["retention_days"], 30)
        self.assertEqual(
            {key: summary[key] for key in ("inspected", "deleted", "skipped", "failed", "errors")},
            {"inspected": 2, "deleted": 1, "skipped": 0, "failed": 0, "errors": []},
        )
        self.assertFalse(stale.exists(), "a stale managed log must be pruned")
        self.assertFalse(stale.parent.exists(), "the emptied managed date directory collapses")
        for kept in (fresh, wrong_name, other_shape, stray):
            self.assertTrue(kept.exists(), kept)
        self.assertTrue(fresh.parent.exists())
        self.assertFalse((root / "w-a" / "docker").exists(), "the emptied w-a/docker branch collapses")
        self.assertTrue((root / "w-a").exists(), "worker branches holding unrelated files survive")
        self.assertTrue((root / "w-b" / "native").exists())

    def test_retention_defaults_delegate_to_the_committed_path_policy(self):
        self.assertEqual(ExecutionLogRetention.DEFAULT_RETENTION_DAYS, 30)
        self.assertEqual(ExecutionLogRetention.DEFAULT_RETENTION_DAYS, ExecutionLogPaths.DEFAULT_RETENTION_DAYS)
        self.assertEqual(ExecutionLogRetention.MAX_PRUNE_DELETIONS, 1000)
        self.assertEqual(ExecutionLogRetention.MAX_WALK_DEPTH, 6)
        try:
            for raw, expected in (("45", 45), ("400", 365), ("0", 1), ("garbage", 30), ("", 30)):
                os.environ["WORKER_EXECUTION_LOG_RETENTION_DAYS"] = raw
                self.assertEqual(ExecutionLogRetention.retention_days(), expected, raw)
        finally:
            os.environ.pop("WORKER_EXECUTION_LOG_RETENTION_DAYS", None)

    def test_explicit_retention_windows_are_clamped_and_invalid_ones_rejected(self):
        for requested, expected in ((400, 365), (0, 1)):
            with self.subTest(requested=requested):
                summary = ExecutionLogRetention.prune(
                    self._temporary / "absent", retention_days=requested
                )
                self.assertEqual(summary["retention_days"], expected)
        for requested in ("30", 30.0, True, False, b"30"):
            with self.subTest(requested=requested):
                with self.assertRaises(ValueError):
                    ExecutionLogRetention.prune(self._temporary, retention_days=requested)

    def test_prune_fails_open_on_a_missing_root_and_never_raises(self):
        summary = ExecutionLogRetention.prune(self._temporary / "absent-root", retention_days=30)
        self.assertEqual(
            {key: summary[key] for key in ("inspected", "deleted", "skipped", "failed", "errors")},
            {"inspected": 0, "deleted": 0, "skipped": 0, "failed": 0, "errors": []},
        )
        self.assertEqual(summary["retention_days"], 30)

    def test_importing_retention_loads_no_capture_facade(self):
        """Import isolation (S1's slice independence) is verified in a fresh
        interpreter, not by asserting the ``sys.modules`` state of this test
        process: a combined test run imports sibling suites first, so global
        module state depends on run composition and never on the retention
        slice itself. The subprocess imports the retention module exactly as
        a real worker would (package mode from the repository root) and
        proves that neither import time nor pruning loads the capture facade
        or the Docker SDK."""
        code = "\n".join(
            [
                "import os, shutil, sys, tempfile",
                "from datetime import datetime, timedelta, timezone",
                "from pathlib import Path",
                "from src.worker_agent.infrastructure.security.execution_log_retention import ExecutionLogRetention",
                "for forbidden in (",
                "    'src.worker_agent.infrastructure.security.execution_log_capture',",
                "    'docker',",
                "):",
                "    assert forbidden not in sys.modules, f'{forbidden} imported at import time'",
                "temporary = Path(tempfile.mkdtemp(prefix='retention-standalone-isolation-'))",
                "when = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()",
                "stale = temporary / 'w' / 'docker' / '20200101' / 'job-1234567890.log'",
                "stale.parent.mkdir(parents=True)",
                "stale.write_text('evidence', encoding='utf-8')",
                "os.utime(stale, (when, when))",
                "summary = ExecutionLogRetention.prune(temporary, retention_days=30)",
                "assert summary['deleted'] == 1, summary",
                "assert summary['failed'] == 0, summary",
                "assert not stale.exists(), 'the stale managed log must be pruned'",
                "for forbidden in (",
                "    'src.worker_agent.infrastructure.security.execution_log_capture',",
                "    'docker',",
                "):",
                "    assert forbidden not in sys.modules, f'prune imported {forbidden}'",
                "print('OK', summary['retention_days'], summary['deleted'], summary['failed'])",
                "shutil.rmtree(temporary, ignore_errors=True)",
            ]
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("OK 30 1 0", completed.stdout.strip())

    def test_flat_import_mode_is_self_contained_with_exit_zero(self):
        """The module must also work with its containing directory as
        ``sys.path[0]`` (frozen workers and source-script children share
        that layout), importing no capture module and exiting zero."""
        code = "\n".join(
            [
                "import os, shutil, sys, tempfile",
                "from datetime import datetime, timedelta, timezone",
                "from pathlib import Path",
                "from execution_log_retention import ExecutionLogRetention",
                "assert 'execution_log_capture' not in sys.modules, 'capture imported'",
                "temporary = Path(tempfile.mkdtemp(prefix='flat-retention-standalone-'))",
                "when = (datetime.now(timezone.utc) - timedelta(days=40)).timestamp()",
                "stale = temporary / 'worker-1' / 'docker' / '20200101' / 'job-1234567890.log'",
                "stale.parent.mkdir(parents=True)",
                "stale.write_text('evidence', encoding='utf-8')",
                "os.utime(stale, (when, when))",
                "summary = ExecutionLogRetention.prune(temporary, retention_days=30)",
                "assert 'execution_log_capture' not in sys.modules, 'prune imported capture'",
                "print('OK', summary['retention_days'], summary['deleted'], summary['failed'])",
                "shutil.rmtree(temporary, ignore_errors=True)",
            ]
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_MODULE_DIRECTORY),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("OK 30 1 0", completed.stdout.strip())


class ExecutionLogRetentionStandaloneSwapTests(unittest.TestCase):
    """S5 race containment modelled deterministically between the sweep and
    candidate deletion: a swap that redirects the candidate path outside the
    configured root must never redirect the deletion (S4 failure reporting
    asserted with exact counts where deterministic)."""

    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="retention-standalone-swap-"))

    def tearDown(self):
        shutil.rmtree(self._temporary, ignore_errors=True)
        os.environ.pop("WORKER_EXECUTION_LOG_ROOT", None)
        os.environ.pop("WORKER_EXECUTION_LOG_RETENTION_DAYS", None)

    @staticmethod
    def _stale(root: Path, relative: str, *, days: int = 35, kind: str = "evidence") -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(kind, encoding="utf-8")
        stamp = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def _prune_with_ancestor_swap(self, link_factory: Callable[[Path, Path], None]):
        """One ``prune`` where the worker directory is replaced right after
        the sweep, so the candidate path resolves to a stale victim log
        outside the configured root."""
        root = self._temporary / "logs"
        victim_tree = self._temporary / "victim-tree"
        self._stale(root, "w/docker/20200101/job-old-1700000000.log")
        victim = self._stale(victim_tree, "docker/20200101/job-old-1700000000.log", kind="VICTIM")
        original_sweep = ExecutionLogRetention._sweep
        performed = []
        ancestor = root / "w"

        def sweep_then_swap(directory, cutoff, summary):
            result = original_sweep(directory, cutoff, summary)
            if not performed:
                performed.append(True)
                shutil.rmtree(ancestor)
                link_factory(victim_tree, ancestor)
            return result

        with unittest.mock.patch.object(ExecutionLogRetention, "_sweep", sweep_then_swap):
            summary = ExecutionLogRetention.prune(root, retention_days=30)
        return summary, victim, ancestor

    @unittest.skipUnless(sys.platform == "win32", "NTFS junctions are Windows specific")
    def test_prune_fails_closed_when_a_worker_directory_becomes_an_ntfs_junction(self):
        import _winapi

        summary, victim, junction = self._prune_with_ancestor_swap(
            lambda target, link: _winapi.CreateJunction(str(target.resolve()), str(link))
        )
        self.assertEqual(summary["deleted"], 0)
        self.assertGreaterEqual(summary["failed"], 1)
        self.assertTrue(summary["errors"], "containment failure must be reported")
        self.assertTrue(victim.exists(), "victim log outside the root was deleted")
        self.assertEqual(victim.read_text(encoding="utf-8"), "VICTIM")
        self.assertTrue(junction.is_dir(), "junction link was removed")

    @unittest.skipUnless(_POSIX, "directory symbolic links are POSIX specific")
    def test_prune_fails_closed_when_a_worker_directory_becomes_a_symlink(self):
        summary, victim, symlink = self._prune_with_ancestor_swap(os.symlink)
        self.assertEqual(summary["deleted"], 0)
        self.assertGreaterEqual(summary["failed"], 1)
        self.assertTrue(summary["errors"], "containment failure must be reported")
        self.assertTrue(victim.exists(), "victim log outside the root was deleted")
        self.assertEqual(victim.read_text(encoding="utf-8"), "VICTIM")
        self.assertTrue(os.path.islink(symlink), "symbolic link was removed")

    @unittest.skipUnless(sys.platform == "win32", "NTFS junctions are Windows specific")
    def test_prune_fails_closed_when_the_root_is_replaced_by_an_ntfs_junction(self):
        import _winapi

        root = self._temporary / "logs"
        self._stale(root, "w/docker/20200101/job-old-1700000000.log", kind="EVIDENCE")
        original_sweep = ExecutionLogRetention._sweep
        performed = []
        state = {}

        def sweep_then_swap(directory, cutoff, summary):
            result = original_sweep(directory, cutoff, summary)
            if not performed:
                performed.append(True)
                renamed = root.with_name("logs-moved")
                os.rename(root, renamed)
                victim = self._stale(
                    self._temporary / "replaced-root",
                    "w/docker/20200101/job-old-1700000000.log",
                    kind="VICTIM",
                )
                _winapi.CreateJunction(
                    str((self._temporary / "replaced-root").resolve()), str(root)
                )
                state.update(renamed=renamed, victim=victim)
            return result

        with unittest.mock.patch.object(ExecutionLogRetention, "_sweep", sweep_then_swap):
            summary = ExecutionLogRetention.prune(root, retention_days=30)
        self.assertEqual(summary["deleted"], 0)
        self.assertGreaterEqual(summary["failed"], 1)
        self.assertTrue(summary["errors"], "containment failure must be reported")
        self.assertTrue(state["renamed"].is_dir(), "renamed origin must stay intact")
        self.assertNotEqual(
            getattr(os.lstat(root), "st_reparse_tag", 0),
            0,
            "the root junction link was removed",
        )
        self.assertTrue(state["victim"].exists(), "victim log was deleted through the junction")
        self.assertEqual(state["victim"].read_text(encoding="utf-8"), "VICTIM")
        self.assertTrue(state["renamed"].is_dir(), "renamed origin must stay intact")

    @unittest.skipUnless(
        _POSIX and _pinned_fd_resolves_after_rename(),
        "exact counts are only deterministic where the pinned root descriptor "
        "still resolves after the root rename (DrvFs fail-closed outcome is "
        "covered by tests/test_execution_log_retention.py)",
    )
    def test_prune_never_deletes_through_a_root_replaced_by_a_symlink(self):
        """The pinned root object survives a root rename + symlink swap: the
        original candidate is pruned through the pinned descriptor while the
        replacement tree is never opened or touched."""
        root = self._temporary / "logs"
        self._stale(root, "w/docker/20200101/job-old-1700000000.log", kind="EVIDENCE")
        original_sweep = ExecutionLogRetention._sweep
        performed = []
        state = {}

        def sweep_then_swap(directory, cutoff, summary):
            result = original_sweep(directory, cutoff, summary)
            if not performed:
                performed.append(True)
                replacement = self._temporary / "replaced-root"
                replacement_file = self._stale(
                    replacement, "w/docker/20200101/job-old-1700000000.log", kind="REPLACEMENT"
                )
                renamed = root.with_name("logs-moved")
                os.rename(root, renamed)
                os.symlink(replacement, root)
                state.update(renamed=renamed, replacement_file=replacement_file)
            return result

        with unittest.mock.patch.object(ExecutionLogRetention, "_sweep", sweep_then_swap):
            summary = ExecutionLogRetention.prune(root, retention_days=30)
        original = state["renamed"] / "w" / "docker" / "20200101" / "job-old-1700000000.log"
        self.assertEqual(
            {key: summary[key] for key in ("deleted", "skipped", "failed")},
            {"deleted": 1, "skipped": 0, "failed": 0},
            summary,
        )
        self.assertEqual(summary["errors"], [])
        self.assertFalse(original.exists(), "the pinned origin itself must be pruned")
        self.assertEqual(
            state["replacement_file"].read_text(encoding="utf-8"),
            "REPLACEMENT",
            "a file reached only through the replacement symlink was deleted",
        )
        self.assertTrue(os.path.islink(root))


if __name__ == "__main__":
    unittest.main()