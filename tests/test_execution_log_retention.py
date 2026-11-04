"""Independent behavior tests for the execution-log retention component.

S3 of ``odd/tasks/persistent-worker-execution-logs.md``: managed job logs are
pruned after the default 30-day retention window; pruning only ever deletes
files matching the exact fixed layout the committed path policy
(:class:`ExecutionLogPaths`) produces, never follows symbolic links into or
outside the root, and never raises - every failure is reported in a summary
(S4) so callers continue and alert.

The tests here exercise the standalone :class:`ExecutionLogRetention` module
directly (not through the capture facade) including its source-script
(flat) import mode, so the extraction from the capture prototype stays
independently verifiable.

Tests whose precondition is a platform capability are deterministically
probed against the filesystem that backs the fixtures' temp directories
(``tempfile.mkdtemp``): directory permission-bit enforcement (ext4 enforces
it; Windows and WSL ``/mnt/c`` DrvFs without the ``metadata`` mount option
do not) and whether a descriptor pinned on the configured root still
resolves named children after the root is renamed (ext4 yes; DrvFs fails
``openat`` from the stale descriptor, which is itself a legitimate fail-
closed containment outcome asserted exactly in the root-boundary swap
suite).
"""

import functools
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

_POSIX = os.name != "nt"
_MODULE_DIRECTORY = Path(__file__).resolve().parents[1] / "src" / "worker_agent" / "infrastructure" / "security"

try:
    from src.worker_agent.infrastructure.security.execution_log_capture import ExecutionLogCapture
    from src.worker_agent.infrastructure.security.execution_log_retention import ExecutionLogRetention
except ModuleNotFoundError:  # pragma: no cover - direct execution from tests/
    from src.worker_agent.infrastructure.security.execution_log_retention import ExecutionLogRetention  # type: ignore[no-redef]


@functools.lru_cache(maxsize=1)
def _permission_bits_enforced() -> bool:
    """Deterministically detect whether POSIX directory permission bits are
    actually enforced on the filesystem backing the tests' temp directories.

    The chmod-based denial tests state their precondition with permission
    bits, and they are only meaningful where the platform enforces them:
    Linux ext4 enforces them, Windows does not use them, and WSL ``/mnt/c``
    (DrvFs without the ``metadata`` mount option) accepts ``os.chmod`` but
    never denies access. The probe is exercised on exactly the same
    filesystem that ``tempfile.mkdtemp`` uses for the fixtures of this
    process, so a run whose temp directory lands on an unenforcing volume
    deterministically skips those tests instead of failing on the assumed
    capability. A privileged ``root`` user bypasses the bits on enforcing
    filesystems too, and the same probe observes that directly.
    """
    if os.name == "nt":
        return False
    envelope = Path(tempfile.mkdtemp(prefix="retention-denial-probe-"))
    probe = envelope / "probe-directory"
    probe.mkdir()
    (probe / "seed").write_text("seed", encoding="utf-8")
    try:
        os.chmod(probe, 0o500)
        try:
            (probe / "denied-write").write_text("denied", encoding="utf-8")
        except PermissionError:
            return True
        return False
    finally:
        try:
            os.chmod(probe, 0o755)
        except OSError:
            pass
        shutil.rmtree(envelope, ignore_errors=True)


@functools.lru_cache(maxsize=1)
def _pinned_directory_fd_resolves_after_rename() -> bool:
    """Whether a descriptor pinned on a directory still resolves the
    directory's named children after the directory itself is renamed.

    The POSIX root-swap tests model the configured root being renamed away
    between the sweep and the deletion, exactly the point where the
    containment boundary descriptor is already pinned. Linux ext4 keeps
    resolving the pinned object by name afterwards, while DrvFs under WSL
    ``/mnt/c`` fails ``openat`` from the stale descriptor once the parent
    has been renamed, so on that filesystem the containment walk itself
    fails closed (reported in the summary, nothing touched) and the
    identity-comparison paths those tests exercise are never reached. The
    probe is deterministic per backing filesystem, decided once per process.
    """
    if os.name == "nt":
        return True  # POSIX-only behavior; Windows tests do not depend on it.
    envelope = Path(tempfile.mkdtemp(prefix="retention-fd-rename-probe-"))
    directory = envelope / "pinned"
    (directory / "child").mkdir(parents=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.rename(directory, directory.with_name("pinned-moved"))
        try:
            os.open("child", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError:
            return False
        return True
    finally:
        os.close(fd)
        shutil.rmtree(envelope, ignore_errors=True)


_PERMISSION_BITS_ENFORCED = _POSIX and _permission_bits_enforced()
_PINNED_FD_RESOLVES_AFTER_RENAME = _POSIX and _pinned_directory_fd_resolves_after_rename()


def _restore_permissions_if_present(path: Path, mode: int) -> None:
    """Restore the permission bits of a guard directory, tolerating a prune
    that already collapsed it: on a filesystem whose permission bits are not
    enforced the denied deletion legitimately succeeds and the emptied
    managed directories (this path included) are removed."""
    try:
        os.chmod(path, mode)
    except FileNotFoundError:
        pass


class ExecutionLogRetentionTests(unittest.TestCase):
    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-retention-"))

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

    # S3: the default 30-day window deletes only stale managed logs and
    # collapses the emptied managed directories.

    def test_prune_enforces_retention_and_collapses_empty_directories(self):
        root = self._temporary / "logs"
        stale_log = self._stale(root, "worker-a/docker/20200101/job-old-1700000000.log")
        fresh_log = self._stale(root, "worker-b/native/20200102/job-fresh-1700000001.log", days=2)
        unrelated = self._stale(root, "worker-a/docker/old-job.txt")
        summary = ExecutionLogRetention.prune(root, retention_days=30)
        self.assertEqual(summary["retention_days"], 30)
        self.assertEqual(summary["inspected"], 2)
        self.assertEqual(summary["deleted"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["errors"], [])
        self.assertFalse(stale_log.exists())
        self.assertTrue(fresh_log.exists())
        self.assertTrue(unrelated.exists())
        self.assertFalse(stale_log.parent.exists())
        self.assertTrue((root / "worker-a").exists())
        self.assertTrue((root / "worker-b" / "native").exists())

    def test_default_retention_is_thirty_days_and_overrides_are_clamped(self):
        self.assertEqual(ExecutionLogRetention.DEFAULT_RETENTION_DAYS, 30)
        self.assertEqual(ExecutionLogRetention.retention_days(), 30)
        try:
            for raw, expected in (("45", 45), ("0", 1), ("400", 365), ("garbage", 30), ("", 30), ("-5", 1)):
                os.environ["WORKER_EXECUTION_LOG_RETENTION_DAYS"] = raw
                self.assertEqual(ExecutionLogRetention.retention_days(), expected, raw)
        finally:
            os.environ.pop("WORKER_EXECUTION_LOG_RETENTION_DAYS", None)

    def test_explicit_retention_days_are_clamped_and_non_integers_rejected(self):
        root = self._temporary / "logs"
        for requested, expected in ((0, 1), (365, 365), (400, 365)):
            with self.subTest(requested=requested):
                summary = ExecutionLogRetention.prune(root / f"clamp-{requested}", retention_days=requested)
                self.assertEqual(summary["retention_days"], expected)
        for requested in ("30", 30.0, True, False, b"30"):
            with self.subTest(requested=requested):
                with self.assertRaises(ValueError):
                    ExecutionLogRetention.prune(root, retention_days=requested)

    # S3/S4: pruning never raises; every failure is summarized.

    def test_prune_fails_open_on_a_missing_root_and_reports_the_default_window(self):
        summary = ExecutionLogRetention.prune(self._temporary / "absent-root")
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["retention_days"], ExecutionLogRetention.DEFAULT_RETENTION_DAYS)

    def test_prune_without_a_root_uses_the_env_fixed_host_location(self):
        root = self._temporary / "env-root"
        stale_log = self._stale(root, "worker-1/docker/20200101/job-1234567890.log")
        os.environ["WORKER_EXECUTION_LOG_ROOT"] = str(root)
        summary = ExecutionLogRetention.prune()
        self.assertEqual(summary["deleted"], 1)
        self.assertFalse(stale_log.exists())

    @unittest.skipUnless(
        _PERMISSION_BITS_ENFORCED, "directory permission-bit denial is not enforced on this filesystem"
    )
    def test_prune_counts_failures_without_raising(self):
        root = self._temporary / "logs"
        locked = self._stale(root, "locked/docker/20200101/job-stale-1234567890.log")
        os.chmod(root / "locked" / "docker" / "20200101", 0o555)
        try:
            summary = ExecutionLogRetention.prune(root, retention_days=30)
        finally:
            _restore_permissions_if_present(root / "locked" / "docker" / "20200101", 0o755)
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["failed"], 1)
        self.assertTrue(locked.exists())

    # Security invariant: only the managed layout is removable, and symbolic
    # links are never followed when inspecting, pruning or collapsing.

    def test_prune_never_removes_files_outside_the_managed_log_layout(self):
        root = self._temporary / "logs"
        managed = self._stale(root, "worker-a/docker/20200101/job-1234567890.log")
        managed_suffix = self._stale(root, "worker-a/native/20200102/job-1234567890-1.log")
        unmanaged_name = self._stale(root, "worker-a/docker/20200101/app.log")
        stale_but_unshaped = self._stale(root, "worker-a/docker/20200101/2020010112.log")
        shallow = self._stale(root, "system.log")
        nested_unmanaged = self._stale(root, "other/nested/service.log")
        empty_unmanaged = root / "other" / "empty-dir"
        empty_unmanaged.mkdir()
        summary = ExecutionLogRetention.prune(root, retention_days=30)
        self.assertEqual(summary["inspected"], 2)
        self.assertEqual(summary["deleted"], 2)
        self.assertFalse(managed.exists())
        self.assertFalse(managed_suffix.exists())
        for kept in (unmanaged_name, stale_but_unshaped, shallow, nested_unmanaged):
            self.assertTrue(kept.exists(), kept)
        self.assertTrue(empty_unmanaged.exists())
        self.assertTrue(unmanaged_name.parent.exists())
        self.assertFalse(managed_suffix.parent.exists())
        self.assertFalse((root / "worker-a" / "native").exists())

    @unittest.skipUnless(_POSIX, "symbolic links are POSIX specific")
    def test_prune_never_follows_symbolic_links(self):
        outside = self._temporary / "outside-managed-tree"
        stale_managed_outside = self._stale(outside, "outside-worker/docker/20200101/job-1234567890.log")
        plain_outside = self._stale(outside, "plain.txt")
        root = self._temporary / "logs"
        stale_symlink_file = self._stale(root, "worker-a/docker/20200102/real-1234567890.log")
        linked_file = root / "worker-a" / "docker" / "20200101" / "job-1234567890.log"
        linked_file.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(plain_outside, linked_file)
        # A link inside the root to a directory tree full of stale managed
        # logs must not become prunable evidence or a traversal path.
        os.symlink(outside, stale_symlink_file.parent / "escape")
        symlinked_dir = root / "worker-b" / "native"
        symlinked_dir.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, symlinked_dir)
        summary = ExecutionLogRetention.prune(root, retention_days=30)
        self.assertEqual(summary["deleted"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertTrue(plain_outside.exists())
        self.assertTrue(stale_managed_outside.exists())
        self.assertTrue(linked_file.exists())
        # The only deletion candidate is the genuinely stale managed log at
        # 20200102 (the other managed-named entry is a symbolic link and is
        # never a candidate): correct retention behavior prunes it, so it is
        # asserted deleted while every link and the outside tree survive.
        self.assertFalse(stale_symlink_file.exists())
        self.assertTrue(outside.exists())
        self.assertTrue(stale_symlink_file.parent.exists())

    # The walk never descends past its bounded depth, so an oversized
    # unmanaged tree cannot stall retention.

    def test_prune_stops_at_the_walk_depth_bound(self):
        root = self._temporary / "logs"
        deep = self._stale(root, "/".join(f"level-{n}" for n in range(8)) + "/job-1234567890.log")
        summary = ExecutionLogRetention.prune(root, retention_days=30)
        self.assertEqual(summary["deleted"], 0)
        self.assertTrue(deep.exists())

    # Source-script (flat) import mode: the standalone component must work
    # when its containing directory is ``sys.path[0]`` (capture child's
    # source-script mode shares this sibling layout, frozen workers package
    # the same module).

    def test_retention_component_imports_and_prunes_in_flat_source_mode(self):
        code = "\n".join(
            [
                "from datetime import datetime, timedelta, timezone",
                "from pathlib import Path",
                "import os, shutil, tempfile",
                "from execution_log_retention import ExecutionLogRetention",
                "",
                "temporary = Path(tempfile.mkdtemp(prefix='flat-retention-'))",
                "when = datetime.now(timezone.utc) - timedelta(days=40)",
                'stale = temporary / "worker-1" / "docker" / "20200101" / "job-1234567890.log"',
                "stale.parent.mkdir(parents=True)",
                "stale.write_text('evidence', encoding='utf-8')",
                "import os; os.utime(stale, (when.timestamp(), when.timestamp()))",
                "summary = ExecutionLogRetention.prune(temporary, retention_days=30)",
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


class ExecutionLogRetentionAncestorSwapTests(unittest.TestCase):
    """Critical race containment: after ``_sweep`` marks a stale managed log
    as a candidate, replacing an ancestor managed directory with an NTFS
    junction (Windows) or a symbolic link (POSIX) must never let pruning
    delete the stale log the candidate path now resolves to outside the
    configured root. Leaf-only metadata rechecks cannot see a replaced
    ancestry, so deletion must be verified as contained by a mechanism that
    does not depend on path traversal staying unchanged (Windows: the
    handle's true final path inside the root's true final path; POSIX: an
    ``O_NOFOLLOW`` component walk from a pinned root descriptor). The race
    is modelled deterministically exactly between sweep and candidate
    deletion, and deletion must fail closed: victim bytes, the junction or
    symlink link and the summary counts are asserted, with the containment
    failure reported (S4) instead of silently tolerated.
    """

    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-swap-"))

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

    def _prune_with_ancestor_swap(self, link_factory):
        """Run one ``prune`` where the worker directory is replaced right
        after the sweep marks the stale managed log, redirecting the candidate
        path onto a stale victim log outside the configured root."""
        root = self._temporary / "logs"
        victim_tree = self._temporary / "victim-tree"
        candidate_relative = "w/docker/20200101/job-old-1700000000.log"
        self._stale(root, candidate_relative)
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
    def test_prune_fails_closed_when_a_worker_directory_becomes_an_ntfs_junction_after_the_sweep(self):
        import _winapi

        def link_factory(target: Path, link_path: Path):
            _winapi.CreateJunction(str(target.resolve()), str(link_path))

        summary, victim, junction = self._prune_with_ancestor_swap(link_factory)
        self.assertEqual(summary["deleted"], 0)
        self.assertGreaterEqual(summary["failed"], 1)
        self.assertTrue(summary["errors"], "containment failure must be reported")
        self.assertTrue(victim.exists(), "victim log outside the root was deleted through the junction")
        self.assertEqual(victim.read_text(encoding="utf-8"), "VICTIM")
        self.assertTrue(junction.is_dir(), "junction link was removed")
        self.assertNotEqual(getattr(os.lstat(junction), "st_reparse_tag", 0), 0)

    @unittest.skipUnless(_POSIX, "directory symbolic links beyond junctions are POSIX-specific here")
    def test_prune_fails_closed_when_a_worker_directory_becomes_a_symlink_after_the_sweep(self):
        def link_factory(target: Path, link_path: Path):
            os.symlink(target, link_path)

        summary, victim, symlink = self._prune_with_ancestor_swap(link_factory)
        self.assertEqual(summary["deleted"], 0)
        self.assertGreaterEqual(summary["failed"], 1)
        self.assertTrue(summary["errors"], "containment failure must be reported")
        self.assertTrue(victim.exists(), "victim log outside the root was deleted through the symlink")
        self.assertEqual(victim.read_text(encoding="utf-8"), "VICTIM")
        self.assertTrue(os.path.islink(symlink), "symbolic link was removed")
        self.assertTrue(victim.parent.parent.parent.exists())


class ExecutionLogRetentionRootBoundaryTests(unittest.TestCase):
    """Root-boundary containment: after the sweep selects candidates, the
    configured root path itself can be renamed away and replaced with a
    symlink (POSIX) to a tree containing a hard link to the candidate;
    re-opening the root by path per deletion would migrate the deletion into
    the replacement tree. The root directory object is therefore pinned once
    per ``prune``, deletion and collapse walk only through ``O_NOFOLLOW``
    from that pinned descriptor, and candidate identity checks stop a
    mid-sweep swap from deleting a different file. Deletion must never touch
    the replacement tree (the hard link and its bytes remain), the renamed
    origin is still pruned correctly, operators' legitimately symlinked roots
    keep working, and on Windows the per-prune final-path boundary refuses to
    delete through a junction that replaces the root.
    """

    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-root-"))

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

    def _prune_with_root_swap(self, swap):
        """Run one ``prune`` where the configured root path is replaced right
        after the sweep, exactly between sweep and candidate deletion."""
        root = self._temporary / "logs"
        original = root / "w" / "docker" / "20200101" / "job-old-1700000000.log"
        self._stale(root, "w/docker/20200101/job-old-1700000000.log", kind="EVIDENCE")
        original_sweep = ExecutionLogRetention._sweep
        performed = []

        def sweep_then_swap(directory, cutoff, summary):
            result = original_sweep(directory, cutoff, summary)
            if not performed:
                performed.append(True)
                swap(root, original)
            return result

        with unittest.mock.patch.object(ExecutionLogRetention, "_sweep", sweep_then_swap):
            summary = ExecutionLogRetention.prune(root, retention_days=30)
        return summary

    @unittest.skipUnless(_POSIX, "directory symbolic links are POSIX specific")
    def test_prune_never_deletes_through_a_root_replaced_by_a_symlink_after_the_sweep(self):
        state = {}

        def swap(root: Path, original: Path):
            replacement = self._temporary / "replaced-root"
            (replacement / "w" / "docker" / "20200101").mkdir(parents=True)
            hardlink = replacement / "w" / "docker" / "20200101" / "job-old-1700000000.log"
            os.link(original, hardlink)
            renamed = root.with_name("logs-moved")
            os.rename(root, renamed)
            os.symlink(replacement, root)
            state.update(renamed=renamed, replacement=replacement, hardlink=hardlink)

        summary = self._prune_with_root_swap(swap)
        renamed_candidate = state["renamed"] / "w" / "docker" / "20200101" / "job-old-1700000000.log"
        # Which outcomes are reachable depends on one capability of the
        # filesystem backing the fixtures: whether the pinned root
        # descriptor keeps resolving named children after the root rename.
        # Where it does (ext4), the deletion walk reaches the candidate and
        # the two pre-existing outcomes hold; where it does not (WSL /mnt/c
        # DrvFs), the containment walk itself fails closed and that is the
        # only possible outcome — asserted exactly, with every byte safe.
        # The security property asserted for ALL outcomes: the candidate
        # reached only through the replacement symlink, outside the pinned
        # root object, is byte-identical and the link is intact.
        hardlink = state["hardlink"]
        if summary["failed"] == 1 and not _PINNED_FD_RESOLVES_AFTER_RENAME:
            # Fail closed (observed deterministically on DrvFs): the
            # containment walk from the pinned descriptor fails, the
            # candidate is never touched and the failure is reported (S4).
            self.assertEqual(summary["skipped"], 0)
            self.assertEqual(summary["errors"], ["job-old-1700000000.log: OSError"])
            self.assertTrue(
                renamed_candidate.exists(), "a fail-closed walk must leave the pinned origin intact"
            )
            self.assertEqual(renamed_candidate.read_text(encoding="utf-8"), "EVIDENCE")
            self.assertEqual(os.lstat(hardlink).st_nlink, 2)
        elif summary["deleted"] == 1:
            # Candidate identity still matched against the pinned origin:
            # the pinned origin was pruned (its link count drops to one).
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["skipped"], 0)
            self.assertFalse(
                renamed_candidate.exists(), "the pinned origin itself must be deleted"
            )
            self.assertEqual(os.lstat(hardlink).st_nlink, 1)
        elif summary["skipped"] == 1:
            # Fail closed (documented): the hard link moved the shared
            # inode's ctime so identity can no longer be proven against the
            # swept candidate; nothing is deleted and no failure is reported.
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["deleted"], 0)
            self.assertEqual(summary["errors"], [])
            self.assertTrue(
                renamed_candidate.exists(), "a safe skip must keep the pinned origin intact"
            )
            self.assertEqual(renamed_candidate.read_text(encoding="utf-8"), "EVIDENCE")
            self.assertEqual(os.lstat(hardlink).st_nlink, 2)
        else:
            self.fail(f"unexpected prune summary (no valid outcome): {summary}")
        self.assertTrue(hardlink.exists(), "candidate outside the root was deleted")
        self.assertFalse(os.path.islink(hardlink), "the hard link itself must stay a real file")
        self.assertEqual(hardlink.read_text(encoding="utf-8"), "EVIDENCE")
        self.assertTrue(os.path.islink(self._temporary / "logs"))

    class _ShiftedCtimeIdentity:
        """Swept identity whose metadata-change time no longer matches the
        live file (the synthetic, deterministic result of creating a hard
        link after the sweep); every other attribute is carried through."""

        def __init__(self, real, st_ctime_ns: int):
            self.st_dev = real.st_dev
            self.st_ino = real.st_ino
            self.st_size = real.st_size
            self.st_ctime_ns = st_ctime_ns
            self.st_mtime = real.st_mtime
            self.st_mode = real.st_mode

    @staticmethod
    def _shifted_ctime_identity(swept):
        return ExecutionLogRetentionRootBoundaryTests._ShiftedCtimeIdentity(
            swept, int(swept.st_ctime_ns) - 1_000
        )

    @unittest.skipUnless(
        _PINNED_FD_RESOLVES_AFTER_RENAME,
        "the pinned root descriptor cannot resolve after the root rename on this filesystem, "
        "so the identity comparison is never reached (that fail-closed walk outcome is "
        "asserted in test_prune_never_deletes_through_a_root_replaced_by_a_symlink_after_the_sweep)",
    )
    def test_prune_fails_closed_when_a_hardlink_moves_the_candidate_ctime_after_the_sweep(self):
        """Deterministic reproduction of the safe changed-ctime branch of the
        previous test: after the sweep, an outside-root hard link to the
        candidate can move the shared inode's metadata-change time, and then
        identity can no longer be proven against the swept candidate, so
        pruning must fail closed (``skipped``, never a failure and never a
        deletion anywhere) while every byte survives untouched."""
        state = {}

        def swap(root: Path, original: Path):
            replacement = self._temporary / "replaced-root"
            (replacement / "w" / "docker" / "20200101").mkdir(parents=True)
            hardlink = replacement / "w" / "docker" / "20200101" / "job-old-1700000000.log"
            os.link(original, hardlink)
            renamed = root.with_name("logs-moved")
            os.rename(root, renamed)
            os.symlink(replacement, root)
            state.update(renamed=renamed, replacement=replacement, hardlink=hardlink)

        original_identity = ExecutionLogRetention._sweep_identity

        def identity_with_shifted_ctime(child_path, info):
            swept = original_identity(child_path, info)
            if swept is None:
                return None
            return self._shifted_ctime_identity(swept)

        with unittest.mock.patch.object(
            ExecutionLogRetention, "_sweep_identity", staticmethod(identity_with_shifted_ctime)
        ):
            summary = self._prune_with_root_swap(swap)

        renamed_candidate = state["renamed"] / "w" / "docker" / "20200101" / "job-old-1700000000.log"
        self.assertEqual(summary["deleted"], 0, "a moved ctime must never be deleted")
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["errors"], [])
        self.assertTrue(
            renamed_candidate.exists(), "a safe skip must keep the pinned origin intact"
        )
        self.assertEqual(renamed_candidate.read_text(encoding="utf-8"), "EVIDENCE")
        hardlink = state["hardlink"]
        self.assertTrue(hardlink.exists(), "candidate outside the root was deleted")
        self.assertEqual(hardlink.read_text(encoding="utf-8"), "EVIDENCE")
        self.assertEqual(os.lstat(hardlink).st_nlink, 2, "the outside-root link must stay intact")
        self.assertTrue(os.path.islink(self._temporary / "logs"))

    @unittest.skipUnless(_POSIX, "directory symbolic links are POSIX specific")
    def test_prune_prunes_through_a_legitimately_symlinked_root(self):
        target = self._temporary / "real-logs"
        stale_log = self._stale(target, "worker-1/docker/20200101/job-1234567890.log")
        root = self._temporary / "logs-link"
        root.symlink_to(target)
        summary = ExecutionLogRetention.prune(root, retention_days=30)
        self.assertEqual(summary["deleted"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertFalse(stale_log.exists())

    @unittest.skipUnless(sys.platform == "win32", "NTFS junctions are Windows specific")
    def test_prune_fails_closed_when_the_selected_root_is_replaced_by_an_ntfs_junction_after_the_sweep(self):
        import _winapi

        state = {}

        def swap(root: Path, original: Path):
            renamed = root.with_name("logs-moved")
            os.rename(root, renamed)
            replacement = self._temporary / "replaced-root"
            victim = self._stale(
                replacement, "w/docker/20200101/job-old-1700000000.log", kind="VICTIM"
            )
            _winapi.CreateJunction(str(replacement.resolve()), str(root))
            state.update(renamed=renamed, replacement=replacement, victim=victim)

        summary = self._prune_with_root_swap(swap)
        self.assertEqual(summary["deleted"], 0)
        self.assertGreaterEqual(summary["failed"], 1)
        self.assertTrue(summary["errors"], "containment failure must be reported")
        self.assertTrue(state["victim"].exists(), "victim log was deleted through the junction")
        self.assertEqual(state["victim"].read_text(encoding="utf-8"), "VICTIM")
        self.assertTrue(state["victim"].parent.parent.parent.is_dir(), "junction link was removed")


class ExecutionLogRetentionLeafIdentityTests(unittest.TestCase):
    """Leaf identity containment: Windows directory enumeration
    (``DirEntry.stat``) exposes no ``st_ino``/``st_dev`` (FindFirstFile data),
    so sweep-time candidate identity used to be absent and replacing the
    stale candidate leaf with a different same-name stale file between the
    sweep and the deletion made pruning delete the replacement. Candidate
    identity is therefore captured at sweep time from the file object itself
    (a plain non-following stat, which never follows the leaf's own reparse
    points; any planted ancestor is still rejected at deletion by the
    containment boundary or the identity mismatch). When identity cannot be
    captured the candidate fails closed. The replacement must remain
    byte-identical and be reported correctly.
    """

    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-leaf-"))

    @staticmethod
    def _stale(root: Path, relative: str, *, days: int = 35, kind: str = "evidence") -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(kind, encoding="utf-8")
        stamp = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def tearDown(self):
        shutil.rmtree(self._temporary, ignore_errors=True)
        os.environ.pop("WORKER_EXECUTION_LOG_ROOT", None)
        os.environ.pop("WORKER_EXECUTION_LOG_RETENTION_DAYS", None)

    def _prune_with_leaf_replacement(self, swap_leaf):
        root = self._temporary / "logs"
        candidate = root / "w" / "docker" / "20200101" / "job-old-1700000000.log"
        self._stale(root, "w/docker/20200101/job-old-1700000000.log", kind="ORIGINAL")
        original_sweep = ExecutionLogRetention._sweep
        performed = []

        def sweep_then_replace(directory, cutoff, summary):
            result = original_sweep(directory, cutoff, summary)
            if not performed:
                performed.append(True)
                swap_leaf(candidate)
            return result

        with unittest.mock.patch.object(ExecutionLogRetention, "_sweep", sweep_then_replace):
            summary = ExecutionLogRetention.prune(root, retention_days=30)
        return summary, candidate

    @unittest.skipUnless(sys.platform == "win32", "Windows DirEntry enumeration behavior")
    def test_prune_never_deletes_a_replaced_leaf_without_sweep_identity(self):
        def swap(candidate: Path):
            os.unlink(candidate)
            candidate.write_text("REPLACED", encoding="utf-8")
            stamp = (datetime.now(timezone.utc) - timedelta(days=35)).timestamp()
            os.utime(candidate, (stamp, stamp))

        summary, candidate = self._prune_with_leaf_replacement(swap)
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["failed"], 0)
        self.assertGreaterEqual(summary["skipped"], 1)
        self.assertTrue(candidate.exists(), "replacement leaf was deleted")
        self.assertEqual(candidate.read_text(encoding="utf-8"), "REPLACED")

    @unittest.skipUnless(_POSIX, "leaf replacement race is POSIX-verified symmetrically")
    def test_prune_keeps_a_replaced_leaf_with_complete_posix_identity(self):
        def swap(candidate: Path):
            # Different length on purpose: filesystems that recycle the freed
            # inode number immediately hand the replacement the swept file's
            # own device and inode, and coarse clocks can collapse the
            # metadata-change times, so the size remains the deterministic
            # discriminator in identity verification.
            os.unlink(candidate)
            candidate.write_text("REPLACED-LONGER", encoding="utf-8")
            stamp = (datetime.now(timezone.utc) - timedelta(days=35)).timestamp()
            os.utime(candidate, (stamp, stamp))

        summary, candidate = self._prune_with_leaf_replacement(swap)
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["failed"], 0)
        self.assertGreaterEqual(summary["skipped"], 1)
        self.assertTrue(candidate.exists())
        self.assertEqual(candidate.read_text(encoding="utf-8"), "REPLACED-LONGER")

    @unittest.skipUnless(
        _POSIX,
        "the FIFO leaf-replacement race is verified on POSIX filesystems"
        " (the no-follow non-blocking leaf open is a POSIX mechanism)",
    )
    def test_prune_never_blocks_on_a_swept_leaf_replaced_by_a_fifo(self):
        """S4 blocker reproduced by an independent verifier: a FIFO replacing
        the swept managed leaf between the sweep and the deletion made the
        contained deletion hang forever (opening a FIFO read-only blocks
        until a writer appears). The leaf open must be non-blocking and the
        nonregular leaf must fail closed (skipped), with the FIFO itself
        untouched and a bounded prune call — this test aborts a hanging
        prune through a timeout alarm instead of hanging the suite."""
        root = self._temporary / "logs"
        candidate = root / "w" / "docker" / "20200101" / "job-old-1700000000.log"
        self._stale(root, "w/docker/20200101/job-old-1700000000.log", kind="ORIGINAL")
        original_sweep = ExecutionLogRetention._sweep
        performed = []

        def sweep_then_fifo(directory, cutoff, summary):
            result = original_sweep(directory, cutoff, summary)
            if not performed:
                performed.append(True)
                os.unlink(candidate)
                os.mkfifo(candidate)
                stamp = (datetime.now(timezone.utc) - timedelta(days=35)).timestamp()
                os.utime(candidate, (stamp, stamp))
            return result

        def alarm_timeout(signum, frame):
            raise TimeoutError("prune hung on a FIFO-replaced swept leaf")

        previous_alarm = signal.signal(signal.SIGALRM, alarm_timeout)
        signal.alarm(30)
        try:
            with unittest.mock.patch.object(ExecutionLogRetention, "_sweep", sweep_then_fifo):
                summary = ExecutionLogRetention.prune(root, retention_days=30)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_alarm)
        self.assertEqual(summary["inspected"], 1)
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["errors"], [])
        leaf_information = os.lstat(candidate)
        self.assertTrue(stat.S_ISFIFO(leaf_information.st_mode), "the FIFO was replaced or removed")
        self.assertEqual(leaf_information.st_nlink, 1)
        self.assertFalse(candidate.is_file(), "the FIFO was removed by pruning")

    # Ordinary deletion must be untouched by the identity capture (the
    # swept candidate itself is still pruned).

    @unittest.skipUnless(sys.platform == "win32", "Windows DirEntry enumeration behavior")
    def test_prune_deletes_the_swept_candidate_when_identity_is_captured(self):
        root = self._temporary / "logs"
        candidate = self._stale(root, "w/docker/20200101/job-old-1700000000.log", kind="ORIGINAL")
        summary = ExecutionLogRetention.prune(root, retention_days=30)
        self.assertEqual(summary["deleted"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["skipped"], 0)
        self.assertFalse(candidate.exists())


class ExecutionLogRetentionReparseTests(unittest.TestCase):
    """Directory reparse points (NTFS junctions included) must never be
    followed, collapsed or removed: every reparse point may point anywhere
    on the volume, so treating one as a regular managed directory lets pruning
    delete stale evidence outside the capture root or erase the junction a
    legitimate consumer placed in the fixed structure.
    """

    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-retention-"))

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

    @staticmethod
    def _create_junction(source: Path, link_path: Path) -> None:
        import _winapi

        link_path.parent.mkdir(parents=True, exist_ok=True)
        _winapi.CreateJunction(str(source.resolve()), str(link_path))

    # A junction that lands at the worker, runtime or date level of the fixed
    # structure is still a reparse point; the sweep must reject it.

    @unittest.skipUnless(sys.platform == "win32", "NTFS junctions are Windows specific")
    def test_prune_never_follows_an_ntfs_junction_at_any_managed_level(self):
        outside = self._temporary / "outside"
        root = self._temporary / "logs"
        cases = {
            0: (root / "w0", outside / "t0" / "w", "docker/20200101/job-1234567890.log"),
            1: (root / "w1" / "docker", outside / "t1" / "docker", "20200101/job-1234567890.log"),
            2: (root / "w2" / "docker" / "20200101", outside / "t2" / "20200101", "job-1234567890.log"),
        }
        junctions, fixtures = {}, {}
        for level, (link_path, target, managed_relative) in cases.items():
            managed = self._stale(target, managed_relative)
            plain = target / "plain.txt"
            plain.write_text("plain", encoding="utf-8")
            self._create_junction(target, link_path)
            junctions[level] = link_path
            fixtures[level] = (target, managed, plain)
        summary = ExecutionLogRetention.prune(root, retention_days=30)
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["inspected"], 0)
        self.assertEqual(summary["failed"], 0)
        for level, (target, managed, plain) in fixtures.items():
            with self.subTest(level=level):
                self.assertTrue(managed.exists(), "junction target evidence deleted")
                self.assertEqual(managed.read_text(encoding="utf-8"), "evidence")
                self.assertTrue(plain.exists(), "junction target plain file deleted")
                self.assertTrue(target.exists())
                self.assertTrue(junctions[level].is_dir(), "junction removed")

    # Empty-directory cleanup must not collapse a junction just because it
    # matches a managed prefix: the junction is never the managed directory.

    @unittest.skipUnless(sys.platform == "win32", "NTFS junctions are Windows specific")
    def test_prune_never_collapses_or_removes_an_ntfs_junction(self):
        outside = self._temporary / "outside-empty"
        outside.mkdir()
        root = self._temporary / "logs"
        junctions = {
            "worker": root / "w-empty",
            "runtime": root / "w2" / "docker",
            "date": root / "w3" / "docker" / "20200101",
        }
        for level, link_path in junctions.items():
            self._create_junction(outside, link_path)
        summary = ExecutionLogRetention.prune(root, retention_days=30)
        self.assertEqual(summary["deleted"], 0)
        for level, link_path in junctions.items():
            with self.subTest(level=level):
                self.assertTrue(link_path.is_dir(), "empty-directory cleanup removed the junction")
        self.assertTrue(outside.exists())

    # A managed-named file that is itself a reparse point is never deleted:
    # unlinking it would only destroy the link, but an unknown reparse tag
    # can point anywhere, so retention is skipped (fail closed).

    @unittest.skipUnless(sys.platform == "win32", "NTFS junctions are Windows specific")
    def test_prune_never_deletes_a_managed_named_file_junction(self):
        outside = self._temporary / "outside-file"
        outside.mkdir()
        (outside / "payload.txt").write_text("payload", encoding="utf-8")
        root = self._temporary / "logs"
        link_path = root / "w" / "docker" / "20200101" / "job-1234567890.log"
        self._create_junction(outside, link_path)
        summary = ExecutionLogRetention.prune(root, retention_days=30)
        self.assertEqual(summary["deleted"], 0)
        self.assertTrue(link_path.is_dir())
        self.assertEqual((outside / "payload.txt").read_text(encoding="utf-8"), "payload")

    # POSIX: any unresolved attributes must fail closed (no descent, no
    # deletion, the failure is counted and reported).

    @unittest.skipUnless(
        _PERMISSION_BITS_ENFORCED, "directory permission-bit denial is not enforced on this filesystem"
    )
    def test_prune_fails_closed_when_directory_attributes_cannot_be_established(self):
        root = self._temporary / "logs"
        guarded = root / "w-a"
        self._stale(guarded, "docker/20200101/job-1234567890.log")
        # Read permission without search: the worker directory can be listed
        # but its entries cannot be stat'd or traversed.
        os.chmod(guarded, 0o444)
        try:
            summary = ExecutionLogRetention.prune(root, retention_days=30)
        finally:
            _restore_permissions_if_present(guarded, 0o755)
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["failed"], 1)
        self.assertTrue((guarded / "docker" / "20200101" / "job-1234567890.log").exists())
        self.assertTrue(summary["errors"], "attribute failure must be reported")


class ExecutionLogRetentionDelegationTests(unittest.TestCase):
    """The capture facade must keep its S3 behavior by delegating to this
    module: same summary shape, same deletion decision, never raising."""

    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-retention-"))

    def tearDown(self):
        shutil.rmtree(self._temporary, ignore_errors=True)
        os.environ.pop("WORKER_EXECUTION_LOG_ROOT", None)
        os.environ.pop("WORKER_EXECUTION_LOG_RETENTION_DAYS", None)

    def test_capture_prune_delegates_to_the_retention_component(self):
        from src.worker_agent.infrastructure.security.execution_log_capture import ExecutionLogCapture

        self.assertEqual(ExecutionLogCapture.MAX_PRUNE_DELETIONS, ExecutionLogRetention.MAX_PRUNE_DELETIONS)
        self.assertEqual(ExecutionLogCapture.MAX_WALK_DEPTH, ExecutionLogRetention.MAX_WALK_DEPTH)
        root = self._temporary / "logs"
        now = datetime.now(timezone.utc)
        stale = root / "worker-1" / "docker" / "20200101" / "job-old-1700000000.log"
        stale.parent.mkdir(parents=True)
        stale.write_text("evidence", encoding="utf-8")
        stale_stamp = (now - timedelta(days=35)).timestamp()
        os.utime(stale, (stale_stamp, stale_stamp))
        fresh = self._temporary / "fresh.log"
        fresh.write_text("evidence", encoding="utf-8")
        fresh_stamp = (now - timedelta(days=2)).timestamp()
        os.utime(fresh, (fresh_stamp, fresh_stamp))
        summary = ExecutionLogCapture.prune(root, now=now, retention_days=30)
        self.assertEqual(summary["retention_days"], 30)
        self.assertEqual(summary["inspected"], 1)
        self.assertEqual(summary["deleted"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())


class ExecutionLogRetentionVolumeIdentityTests(unittest.TestCase):
    """Volume-identity normalization: Windows Python 3.12+ ``os.lstat`` fills
    ``st_dev`` with the full 64-bit volume identity, while the deletion
    handle's ``GetFileInformationByHandle`` reports the 32-bit
    ``dwVolumeSerialNumber``, so a raw equality comparison against the
    swept identity would wrongly skip every ordinary same-volume prune
    deletion exactly like a foreign-volume candidate. Identity is compared
    on the volume-serial (32-bit) domain: a same-volume candidate is still
    deleted and a genuinely different volume still fails closed (skipped).
    The file identity itself (inode index, creation time, size) is checked
    unchanged and at full width.
    """

    _WIDENING = 0x78841E2E

    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-volume-"))

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

    class _WidenedIdentity:
        """Sweep identity carrying a widened ``st_dev`` whose low 32 bits
        hold the file's real volume serial (the documented Windows layout).
        Every other identity attribute is carried through at full width."""

        def __init__(self, real, st_dev: int):
            self.st_dev = st_dev
            self.st_ino = real.st_ino
            self.st_size = real.st_size
            self.st_ctime_ns = real.st_ctime_ns
            self.st_mtime = real.st_mtime
            self.st_mode = real.st_mode

    def _prune_with_identity(self, root: Path, candidate: Path, st_dev: int):
        real = os.lstat(candidate)
        widened = self._WidenedIdentity(real, st_dev)
        original_identity = ExecutionLogRetention._sweep_identity
        try:
            with unittest.mock.patch.object(
                ExecutionLogRetention,
                "_sweep_identity",
                staticmethod(lambda child_path, info: widened),
            ):
                return ExecutionLogRetention.prune(root, retention_days=30)
        finally:
            ExecutionLogRetention._sweep_identity = original_identity

    @unittest.skipUnless(sys.platform == "win32", "wider st_dev is a Windows Python 3.12+ behavior")
    def test_prune_deletes_a_same_volume_candidate_whose_identified_volume_serial_is_wider(self):
        root = self._temporary / "logs"
        candidate = self._stale(root, "w/docker/20200101/job-old-1700000000.log", kind="ORIGINAL")
        serial32 = os.lstat(candidate).st_dev & 0xFFFFFFFF
        summary = self._prune_with_identity(root, candidate, serial32 | (self._WIDENING << 32))
        self.assertEqual(summary["deleted"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["skipped"], 0)
        self.assertFalse(candidate.exists(), "the same-volume stale log must still be pruned")

    @unittest.skipUnless(sys.platform == "win32", "wider st_dev is a Windows Python 3.12+ behavior")
    def test_prune_skips_a_candidate_from_a_genuinely_different_volume_serial(self):
        root = self._temporary / "logs"
        candidate = self._stale(root, "w/docker/20200101/job-old-1700000000.log", kind="KEEP")
        different_volume = (os.lstat(candidate).st_dev & 0xFFFFFFFF) ^ 0x5A5A5A5A
        summary = self._prune_with_identity(root, candidate, different_volume | (self._WIDENING << 32))
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["failed"], 0)
        self.assertGreaterEqual(summary["skipped"], 1)
        self.assertTrue(candidate.exists(), "a foreign-volume candidate must fail closed")
        self.assertEqual(candidate.read_text(encoding="utf-8"), "KEEP")


if __name__ == "__main__":
    unittest.main()