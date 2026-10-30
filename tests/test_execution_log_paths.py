"""Behavior tests for the extracted execution-log paths component.

Focused standalone counterpart of the capture foundation (S1, S3 in
``odd/tasks/persistent-worker-execution-logs.md``): the fixed host log
structure with validated components (S1) and the default 30-day retention
configuration with its clamped overrides (S3). The module must stay
self-contained: importing it must not require the capture module, the
Docker SDK or any worker runtime.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.worker_agent.infrastructure.security.execution_log_paths import ExecutionLogPaths

_FIXED_WHEN = datetime(2026, 1, 7, 12, 30, tzinfo=timezone.utc)


class ExecutionLogPathsTests(unittest.TestCase):
    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-paths-"))

    def tearDown(self):
        shutil.rmtree(self._temporary, ignore_errors=True)
        os.environ.pop("WORKER_EXECUTION_LOG_ROOT", None)
        os.environ.pop("WORKER_EXECUTION_LOG_RETENTION_DAYS", None)

    def test_module_is_standalone_without_capture_or_docker(self):
        """The paths slice must not depend on the uncommitted capture module
        or on the Docker SDK: a direct child import resolves it alone."""
        code = "import src.worker_agent.infrastructure.security.execution_log_paths"
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    # Shared contract: the delegated capture keeps reading the same
    # environment variables and runtime kinds; renaming them would be an
    # accepted-behavior change this extraction must not make.

    def test_environment_and_runtime_kinds_are_unchanged(self):
        self.assertEqual(ExecutionLogPaths.ROOT_ENV, "WORKER_EXECUTION_LOG_ROOT")
        self.assertEqual(ExecutionLogPaths.RETENTION_ENV, "WORKER_EXECUTION_LOG_RETENTION_DAYS")
        self.assertEqual(ExecutionLogPaths.RUNTIME_KINDS, frozenset({"docker", "native"}))

    # S1: fixed host structure with validated components.

    def test_job_log_path_uses_fixed_structure_and_rejects_unsafe_components(self):
        root = self._temporary / "logs"
        path = ExecutionLogPaths.job_log_path(root, "worker-7", "docker", "job-abc123", when=_FIXED_WHEN)
        self.assertEqual(
            path.relative_to(root).as_posix(),
            f"worker-7/docker/20260107/job-abc123-{int(_FIXED_WHEN.timestamp())}.log",
        )
        for kind in ("docker", "native"):
            ExecutionLogPaths.job_log_path(root, "worker-7", kind, "job-abc123", when=_FIXED_WHEN)
        with self.assertRaises(ValueError):
            ExecutionLogPaths.job_log_path(root, "worker-7", "kubernetes", "job-abc123", when=_FIXED_WHEN)
        with self.assertRaises(ValueError):
            ExecutionLogPaths.job_log_path(root, "worker-7", "docker", "../escape", when=_FIXED_WHEN)
        with self.assertRaises(ValueError):
            ExecutionLogPaths.job_log_path(root, "with/slash", "docker", "job-abc123", when=_FIXED_WHEN)
        with self.assertRaises(ValueError):
            ExecutionLogPaths.job_log_path(root, "worker-7", "docker", "", when=_FIXED_WHEN)

    def test_job_log_path_defaults_the_timestamp_to_now(self):
        path = ExecutionLogPaths.job_log_path(self._temporary, "worker-7", "native", "job-now")
        self.assertEqual(path.relative_to(self._temporary).parts[:3], ("worker-7", "native", path.parent.name))
        self.assertRegex(path.name, r"^job-now-\d{10}\.log$")

    # S1: the fixed host location itself, with the platform fallbacks.

    def test_default_root_is_the_fixed_host_location(self):
        if os.name == "nt":
            program_data = os.environ.get("PROGRAMDATA", "C:/ProgramData")
            self.assertEqual(
                ExecutionLogPaths.default_root(),
                os.path.join(program_data, "docker-volume-backup", "execution-logs"),
            )
        else:
            self.assertEqual(ExecutionLogPaths.default_root(), "/var/lib/docker-volume-backup/execution-logs")

    def test_default_root_prefers_a_nonblank_environment_override(self):
        custom = str(self._temporary / "custom-root")
        os.environ["WORKER_EXECUTION_LOG_ROOT"] = custom
        self.assertEqual(ExecutionLogPaths.default_root(), custom)
        # A blank override is not a location; the fixed fallback applies.
        os.environ["WORKER_EXECUTION_LOG_ROOT"] = "   "
        if os.name == "nt":
            program_data = os.environ.get("PROGRAMDATA", "C:/ProgramData")
            fallback = os.path.join(program_data, "docker-volume-backup", "execution-logs")
        else:
            fallback = "/var/lib/docker-volume-backup/execution-logs"
        self.assertEqual(ExecutionLogPaths.default_root(), fallback)

    # S3: the 30-day default and its clamped overrides.

    def test_default_retention_is_thirty_days_and_overrides_are_clamped(self):
        self.assertEqual(ExecutionLogPaths.DEFAULT_RETENTION_DAYS, 30)
        self.assertEqual(ExecutionLogPaths.retention_days(), 30)
        try:
            for raw, expected in (
                ("45", 45),
                ("0", 1),
                ("400", 365),
                ("garbage", 30),
                ("", 30),
                (None, 30),
                ("-5", 1),
            ):
                if raw is None:
                    os.environ.pop("WORKER_EXECUTION_LOG_RETENTION_DAYS", None)
                else:
                    os.environ["WORKER_EXECUTION_LOG_RETENTION_DAYS"] = raw
                self.assertEqual(ExecutionLogPaths.retention_days(), expected, raw)
        finally:
            os.environ.pop("WORKER_EXECUTION_LOG_RETENTION_DAYS", None)

    # Path-shape validation feeding retention: only the exact managed layout
    # may be recognised as managed, so retention can never delete anything
    # outside the naming this component produces.

    def test_managed_directory_prefix_accepts_only_the_fixed_layout(self):
        self.assertTrue(ExecutionLogPaths.managed_directory_prefix(("worker-a",)))
        self.assertTrue(ExecutionLogPaths.managed_directory_prefix(("worker-a", "docker")))
        self.assertTrue(ExecutionLogPaths.managed_directory_prefix(("worker-a", "native", "20200102")))
        for rejected in (
            (),
            ("with/slash",),
            (".hidden",),
            ("-leading",),
            ("worker-a", "kubernetes"),
            ("worker-a", "DOCKER"),
            ("worker-a", "docker", "202001"),
            ("worker-a", "docker", "202001022"),
            ("worker-a", "docker", "not-a-date"),
        ):
            self.assertFalse(ExecutionLogPaths.managed_directory_prefix(rejected), rejected)

    def test_managed_log_relative_parts_accept_only_managed_log_names(self):
        self.assertTrue(ExecutionLogPaths.managed_log_relative_parts(("worker-a", "docker", "20200101", "job-1234567890.log")))
        self.assertTrue(ExecutionLogPaths.managed_log_relative_parts(("worker-b", "native", "20200102", "job-1234567890-1.log")))
        for rejected in (
            ("worker-a", "docker", "20200101", "app.log"),
            ("worker-a", "docker", "20200101", "2020010112.log"),
            ("system.log",),
            ("worker-a", "docker", "job.log"),
            ("worker-a", "docker", "20200101", "sub", "job-1234567890.log"),
            ("worker-a", "docker", "20200101", "job.log"),
        ):
            self.assertFalse(ExecutionLogPaths.managed_log_relative_parts(rejected), rejected)

    def test_generated_log_names_are_recognised_at_the_maximum_accepted_length(self):
        """Every log name job_log_path generates from accepted components
        must be recognised as managed, so retention can also prune logs for
        128-character component values (regression: job IDs beyond 101
        characters produced names the managed-name grammar rejected, so
        those files were never pruned after 30 days)."""
        for size in (1, 100, 101, 102, 127, 128):
            path = ExecutionLogPaths.job_log_path(
                self._temporary, "worker-a", "docker", "j" * size, when=_FIXED_WHEN
            )
            parts = path.relative_to(self._temporary).parts
            self.assertTrue(ExecutionLogPaths.managed_directory_prefix(parts[:3]), parts)
            self.assertTrue(ExecutionLogPaths.managed_log_relative_parts(parts), parts)


if __name__ == "__main__":
    unittest.main()