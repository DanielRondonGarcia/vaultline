"""Behavior tests for the POSIX descriptor-relative containment component
(S1-S6 in ``odd/tasks/persistent-worker-execution-logs.md``): root pinning,
the ``O_NOFOLLOW`` component walk, contained file deletion with swept-identity
verification and the contained empty-directory collapse. Every mechanism
fails closed when the capability is missing, the boundary cannot be pinned or
the live object stops matching the swept identity. Fixture behavior is
skipped deterministically wherever ``ExecutionLogPosix.CONTAINED`` says the
mechanism is unavailable (Windows, or a filesystem without support).
"""

import functools
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from src.worker_agent.infrastructure.security.execution_log_posix import ExecutionLogPosix
except ModuleNotFoundError:  # pragma: no cover - direct execution from tests/
    from src.worker_agent.infrastructure.security.execution_log_posix import ExecutionLogPosix  # type: ignore[no-redef]

_MODULE_DIRECTORY = Path(__file__).resolve().parents[1] / "src" / "worker_agent" / "infrastructure" / "security"

_CONTAINED = ExecutionLogPosix.CONTAINED
_CUTOFF = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
_STALE = (datetime.now(timezone.utc) - timedelta(days=35)).timestamp()


@functools.lru_cache(maxsize=1)
def _utime_moves_ctime_ns() -> bool:
    """Whether rewriting a file moves ``st_ctime_ns`` on the filesystem
    backing the tests' temp directories (ext4 tracks it; WSL ``/mnt/c`` DrvFs
    without the ``metadata`` mount option does not, and there the changed-
    ctime identity branch is unreachable while deletion of the otherwise
    indistinguishable object is the correct fail-safe outcome)."""
    envelope = Path(tempfile.mkdtemp(prefix="posix-ctime-probe-"))
    probe = envelope / "probe"
    probe.write_text("x", encoding="utf-8")
    before = os.lstat(probe).st_ctime_ns
    time.sleep(0.02)
    probe.write_text("y", encoding="utf-8")
    after = os.lstat(probe).st_ctime_ns
    shutil.rmtree(envelope, ignore_errors=True)
    return after != before


_UTIME_MOVES_CTIME = _utime_moves_ctime_ns()


def _identity(path: Path) -> os.stat_result:
    """Sweep-time identity of a candidate captured like the retention sweep."""
    return os.lstat(path)


def _make_stale(root: Path, relative: str, kind: str = "evidence") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(kind, encoding="utf-8")
    os.utime(path, (_STALE, _STALE))
    return path


@unittest.skipUnless(_CONTAINED, "descriptor-relative containment is unavailable on this platform")
class ExecutionLogPosixContainmentTests(unittest.TestCase):
    """Every contained deletion and collapse decision, fail-closed."""

    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-posix-"))

    def tearDown(self):
        shutil.rmtree(self._temporary, ignore_errors=True)

    def test_root_descriptor_is_pinned_once_and_fail_closes_when_it_cannot_be(self):
        directory = self._temporary / "logs" / "worker-a"
        directory.mkdir(parents=True)
        fd = ExecutionLogPosix.pin_root(directory)
        try:
            self.assertTrue(os.path.samestat(os.fstat(fd), os.lstat(directory)))
        finally:
            os.close(fd)
        with self.assertRaises(OSError):
            ExecutionLogPosix.pin_root(self._temporary / "absent")

    def test_the_component_walk_opens_only_real_directories(self):
        root = self._temporary / "logs"
        (root / "worker-a" / "native").mkdir(parents=True)
        fd = ExecutionLogPosix.pin_root(root)
        self.assertIsNotNone(fd)
        try:
            chained = ExecutionLogPosix.open_directory_chain(fd, ("worker-a", "native"))
            self.assertNotEqual(chained, fd)
            self.assertTrue(os.path.samestat(os.fstat(chained), os.lstat(root / "worker-a" / "native")))
            os.close(chained)
            # An O_NOFOLLOW component open of a link ancestor never walks it.
            os.symlink(root / "worker-a", root / "link")
            with self.assertRaises(OSError):
                ExecutionLogPosix.open_directory_chain(fd, ("link", "native"))
        finally:
            os.close(fd)
        self.assertTrue(os.path.islink(root / "link"), "the link itself must survive")

    def test_delete_file_prunes_the_verified_stale_object(self):
        root = self._temporary / "logs"
        stale = _make_stale(root, "worker-a/docker/20200101/job-1234567890.log")
        fd = ExecutionLogPosix.pin_root(root)
        self.assertIsNotNone(fd)
        try:
            status, error = ExecutionLogPosix.delete_file(
                fd, ("worker-a", "docker", "20200101", "job-1234567890.log"),
                _identity(stale), _CUTOFF,
            )
            self.assertEqual((status, error), ("deleted", None))
        finally:
            os.close(fd)
        self.assertFalse(stale.exists())
        self.assertTrue(stale.parent.exists())

    def test_delete_file_fails_closed_without_a_pinned_root_descriptor(self):
        root = self._temporary / "logs"
        stale = _make_stale(root, "worker-a/docker/20200101/job-1234567890.log")
        status, error = ExecutionLogPosix.delete_file(
            None, ("worker-a", "docker", "20200101", "job-1234567890.log"),
            _identity(stale), _CUTOFF,
        )
        self.assertEqual(status, "failed")
        self.assertEqual(error, "job-1234567890.log: no pinned root descriptor; deletion fails closed")
        self.assertTrue(stale.exists(), "nothing is deleted without a pinned boundary")

    def test_delete_file_never_deletes_an_object_that_stopped_matching_the_sweep(self):
        root = self._temporary / "logs"
        candidate = _make_stale(root, "worker-a/docker/20200101/job-1234567890.log", kind="ORIGINAL")
        swept = _identity(candidate)
        fd = ExecutionLogPosix.pin_root(root)
        self.assertIsNotNone(fd)
        try:
            parts = ("worker-a", "docker", "20200101", "job-1234567890.log")
            # The size no longer matches the reused identity; a non-regular
            # leaf is never a candidate either.
            candidate.write_text("REPLACED-LONGER", encoding="utf-8")
            status, error = ExecutionLogPosix.delete_file(fd, parts, swept, _CUTOFF)
            self.assertEqual((status, error), ("skipped", None))
            self.assertTrue(candidate.exists())
            self.assertEqual(candidate.read_text(encoding="utf-8"), "REPLACED-LONGER")
            directory = self._temporary / "dirs"
            dirleaf = directory / "worker-a/docker/20200101/job-1234567890.log"
            dirleaf.parent.mkdir(parents=True)
            dirleaf.mkdir()
            dfd = ExecutionLogPosix.pin_root(directory)
            try:
                status, error = ExecutionLogPosix.delete_file(
                    dfd, ("worker-a", "docker", "20200101", "job-1234567890.log"),
                    _identity(dirleaf), _CUTOFF,
                )
                self.assertEqual((status, error), ("skipped", None))
                self.assertTrue(dirleaf.is_dir())
            finally:
                os.close(dfd)
        finally:
            os.close(fd)

    @unittest.skipUnless(
        _UTIME_MOVES_CTIME,
        "whether rewriting a file moves st_ctime_ns here: the changed-ctime branch is otherwise "
        "unreachable and deletion of the indistinguishable object is correct",
    )
    def test_delete_file_never_deletes_when_the_metadata_change_time_moved(self):
        root = self._temporary / "logs"
        candidate = _make_stale(root, "worker-a/docker/20200101/job-1234567890.log", kind="ORIGINAL")
        swept = _identity(candidate)
        fd = ExecutionLogPosix.pin_root(root)
        self.assertIsNotNone(fd)
        try:
            parts = ("worker-a", "docker", "20200101", "job-1234567890.log")
            # Same-size rewrite: only the metadata-change time moved.
            candidate.write_text("REPLACED", encoding="utf-8")
            status, error = ExecutionLogPosix.delete_file(fd, parts, swept, _CUTOFF)
            self.assertEqual((status, error), ("skipped", None))
            self.assertTrue(candidate.exists())
            self.assertEqual(candidate.read_text(encoding="utf-8"), "REPLACED")
        finally:
            os.close(fd)

    def test_delete_file_never_deletes_a_file_touched_back_into_the_window(self):
        root = self._temporary / "logs"
        candidate = _make_stale(root, "worker-a/docker/20200101/job-1234567890.log")
        fd = ExecutionLogPosix.pin_root(root)
        self.assertIsNotNone(fd)
        try:
            parts = ("worker-a", "docker", "20200101", "job-1234567890.log")
            # The identity still matches the touched-back live file (only
            # mtime is fresh): skipped, mtime untouched.
            now_stamp = datetime.now(timezone.utc).timestamp()
            os.utime(candidate, (now_stamp, now_stamp))
            touched = _identity_with_mtime(os.lstat(candidate), _STALE)
            status, error = ExecutionLogPosix.delete_file(fd, parts, touched, _CUTOFF)
            self.assertEqual((status, error), ("skipped", None))
            self.assertTrue(candidate.exists())
            self.assertTrue(candidate.stat().st_mtime >= _CUTOFF)
        finally:
            os.close(fd)

    def test_delete_file_refuses_a_leaf_that_is_a_symbolic_link(self):
        root = self._temporary / "logs"
        plain = self._temporary / "outside" / "plain.txt"
        plain.parent.mkdir(parents=True)
        plain.write_text("payload", encoding="utf-8")
        managed = root / "worker-a/docker/20200101/job-1234567890.log"
        managed.parent.mkdir(parents=True)
        os.symlink(plain, managed)
        fd = ExecutionLogPosix.pin_root(root)
        self.assertIsNotNone(fd)
        try:
            status, error = ExecutionLogPosix.delete_file(
                fd, ("worker-a", "docker", "20200101", "job-1234567890.log"),
                os.lstat(managed), _CUTOFF,
            )
            self.assertEqual(status, "failed")
            self.assertEqual(error, "job-1234567890.log: OSError")
            self.assertTrue(plain.exists(), "the link target outside the root survived")
            self.assertTrue(os.path.islink(managed), "the link itself survived")
        finally:
            os.close(fd)

    def test_delete_file_never_blocks_on_a_swept_leaf_replaced_by_a_fifo(self):
        """A FIFO replacing the swept managed leaf between the sweep and the
        deletion must never hang the pruning caller (an independent verifier
        reproduced an indefinite hang: opening a FIFO read-only blocks until
        a writer appears): the no-follow leaf open is non-blocking and the
        nonregular leaf fails closed (``skipped``), with the FIFO itself
        untouched. The probe runs isolated in a strictly bounded subprocess
        so a regression cannot hang this test suite."""
        code = "\n".join(
            [
                "import os, stat as st, tempfile, shutil, sys",
                "from datetime import datetime, timedelta, timezone",
                "from pathlib import Path",
                "from src.worker_agent.infrastructure.security.execution_log_posix import ExecutionLogPosix",
                "root = Path(tempfile.mkdtemp(prefix='posix-fifo-probe-'))",
                "fifo = root / 'worker-a/docker/20200101/job-1234567890.log'",
                "fifo.parent.mkdir(parents=True)",
                "os.mkfifo(fifo)",
                "stale = (datetime.now(timezone.utc) - timedelta(days=35)).timestamp()",
                "cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()",
                "os.utime(fifo, (stale, stale))",
                "swept = os.lstat(fifo)",
                "fd = ExecutionLogPosix.pin_root(root)",
                "status, error = ExecutionLogPosix.delete_file(",
                "    fd, ('worker-a', 'docker', '20200101', 'job-1234567890.log'),",
                "    swept, cutoff,",
                ")",
                "print('OK', (status, error))",
                "print('FIFO', st.S_ISFIFO(os.lstat(fifo).st_mode), os.lstat(fifo).st_nlink)",
                "os.close(fd); shutil.rmtree(root, ignore_errors=True)",
            ]
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True,
            text=True,
            timeout=30,
            # fail closed if the leaf open ever blocks
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("OK ('skipped', None)", completed.stdout.splitlines()[0])
        self.assertEqual("FIFO True 1", completed.stdout.splitlines()[1])

    def test_delete_file_fails_closed_when_an_ancestor_stops_being_a_real_directory(self):
        root = self._temporary / "logs"
        candidate = _make_stale(root, "worker-a/docker/20200101/job-1234567890.log")
        swept = _identity(candidate)
        fd = ExecutionLogPosix.pin_root(root)
        self.assertIsNotNone(fd)
        try:
            os.rename(root / "worker-a", root / "worker-a-moved")
            os.symlink(self._temporary / "victim-tree", root / "worker-a")
            status, error = ExecutionLogPosix.delete_file(
                fd, ("worker-a", "docker", "20200101", "job-1234567890.log"),
                swept, _CUTOFF,
            )
            # The walk raises on the replaced ancestor; the exact exception
            # class (ELOOP vs ENOTDIR) is filesystem-dependent.
            self.assertEqual(status, "failed")
            self.assertRegex(error, r"^job-1234567890\.log: \w+Error$")
        finally:
            os.close(fd)
        renamed = self._temporary / "logs" / "worker-a-moved" / "docker" / "20200101" / "job-1234567890.log"
        self.assertTrue(renamed.exists(), "the pinned origin must stay intact")
        self.assertEqual(renamed.read_text(encoding="utf-8"), "evidence")

    def test_remove_directory_collapses_only_real_emptied_directories(self):
        root = self._temporary / "logs"
        emptied = root / "worker-a" / "docker" / "20200101"
        emptied.mkdir(parents=True)
        occupied = root / "worker-b" / "docker" / "20200101"
        (root / "worker-b" / "docker" / "20200101").mkdir(parents=True)
        (root / "worker-b" / "docker" / "20200101" / "kept.txt").write_text("kept", encoding="utf-8")
        linked = root / "worker-c" / "docker" / "20200101"
        linked.parent.mkdir(parents=True)
        os.symlink(emptied, linked)
        outside = self._temporary / "outside-empty"
        outside.mkdir()
        fd = ExecutionLogPosix.pin_root(root)
        self.assertIsNotNone(fd)
        try:
            self.assertTrue(
                ExecutionLogPosix.remove_directory(fd, ("worker-a", "docker", "20200101"))
            )
            self.assertFalse(emptied.exists())
            self.assertFalse(
                ExecutionLogPosix.remove_directory(fd, ("worker-b", "docker", "20200101"))
            )
            self.assertTrue((occupied / "kept.txt").exists())
            self.assertFalse(
                ExecutionLogPosix.remove_directory(fd, ("worker-c", "docker", "20200101"))
            )
            self.assertTrue(os.path.islink(linked), "a symlink is never collapsed")
            # Without a pinned descriptor nothing is removed (fail closed).
            self.assertFalse(
                ExecutionLogPosix.remove_directory(None, ("outside-empty",))
            )
            self.assertTrue(outside.exists())
        finally:
            os.close(fd)


def _identity_with_mtime(real: os.stat_result, st_mtime: float):
    """Synthetic swept identity whose mtime no longer matches the live file
    (the mtime-vs-cutoff branch is otherwise unreachable on POSIX: the size
    and ctime checks fire first for any observable change)."""
    clone = _SweptIdentity()
    clone.st_dev = real.st_dev
    clone.st_ino = real.st_ino
    clone.st_size = real.st_size
    clone.st_ctime_ns = real.st_ctime_ns
    clone.st_mtime = st_mtime
    clone.st_mode = real.st_mode
    return clone


class _SweptIdentity:
    pass


class ExecutionLogPosixStandaloneTests(unittest.TestCase):
    """Standalone and source-script (flat) import modes."""

    def test_module_is_standalone_without_capture_or_docker(self):
        code = "import src.worker_agent.infrastructure.security.execution_log_posix"
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_flat_import_mode_fails_closed_without_a_pinned_root_descriptor(self):
        code = "\n".join(
            [
                "from execution_log_posix import ExecutionLogPosix",
                "print('OK', ExecutionLogPosix.delete_file(None, ('x',), None, 0.0))",
            ]
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_MODULE_DIRECTORY),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            "OK ('failed', 'x: no pinned root descriptor; deletion fails closed')",
            completed.stdout.strip(),
        )


if __name__ == "__main__":
    unittest.main()