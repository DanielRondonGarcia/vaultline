"""Behavior tests for the persistent execution log capture foundation.

Evidence rules under test (S1-S6, odd/tasks/persistent-worker-execution-logs.md):
every executor run leaves redacted, bounded output on the host under a fixed
structure (S1) shared by Docker and native runtimes (S2), pruned after the
default 30-day retention (S3), failing open when persistence is unavailable
(S4), captured crash-resistantly by a detached child (S5), with Windows
ACL-before-write, long-URL split redaction and frozen dispatch covered by the
security regressions (S6).
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

try:
    import docker.constants  # noqa: F401 the SDK seam stays fake-able w/o SDK
    import docker.utils  # noqa: F401

    _DOCKER_SDK = True
except ModuleNotFoundError:
    _DOCKER_SDK = False

from src.worker_agent.infrastructure.security.execution_log_capture import (
    CAPTURE_CHILD_EXIT_DAEMON_FAILURE,
    CAPTURE_CHILD_EXIT_RECIPE_FAILURE,
    DockerContainerLogSource,
    ExecutionLogCapture,
    ExecutionLogCaptureUnavailable,
    ExecutionLogWriter,
    run_capture_child,
)
from src.worker_agent.infrastructure.security import execution_log_capture as capture_module

_CAPTURE_SCRIPT = os.path.abspath(capture_module.__file__)

_POSIX = os.name != "nt"
_FIXED_WHEN = datetime(2026, 1, 7, 12, 30, tzinfo=timezone.utc)


def _payload(content: str) -> str:
    """Log content without the timestamped event marker lines."""
    return "".join(line + "\n" for line in content.splitlines() if not line.startswith("[2"))


class ExecutionLogCaptureTests(unittest.TestCase):
    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-capture-"))

    def tearDown(self):
        shutil.rmtree(self._temporary, ignore_errors=True)
        os.environ.pop("WORKER_EXECUTION_LOG_ROOT", None)
        os.environ.pop("WORKER_EXECUTION_LOG_RETENTION_DAYS", None)

    # S1: fixed host structure with validated components.

    def test_job_log_path_uses_fixed_structure_and_rejects_unsafe_components(self):
        root = self._temporary / "logs"
        path = ExecutionLogCapture.job_log_path(
            root, "worker-7", "docker", "job-abc123", when=_FIXED_WHEN
        )
        self.assertEqual(
            path.relative_to(root).as_posix(),
            f"worker-7/docker/20260107/job-abc123-{int(_FIXED_WHEN.timestamp())}.log",
        )
        for kind in ("docker", "native"):
            ExecutionLogCapture.job_log_path(root, "worker-7", kind, "job-abc123", when=_FIXED_WHEN)
        with self.assertRaises(ValueError):
            ExecutionLogCapture.job_log_path(root, "worker-7", "kubernetes", "job-abc123", when=_FIXED_WHEN)
        with self.assertRaises(ValueError):
            ExecutionLogCapture.job_log_path(root, "worker-7", "docker", "../escape", when=_FIXED_WHEN)
        with self.assertRaises(ValueError):
            ExecutionLogCapture.job_log_path(root, "with/slash", "docker", "job-abc123", when=_FIXED_WHEN)
        with self.assertRaises(ValueError):
            ExecutionLogCapture.job_log_path(root, "worker-7", "docker", "", when=_FIXED_WHEN)

    def test_default_root_is_the_fixed_host_location(self):
        if os.name == "nt":
            program_data = os.environ.get("PROGRAMDATA", "C:/ProgramData")
            self.assertEqual(
                ExecutionLogCapture.default_root(),
                os.path.join(program_data, "docker-volume-backup", "execution-logs"),
            )
        else:
            self.assertEqual(
                ExecutionLogCapture.default_root(),
                "/var/lib/docker-volume-backup/execution-logs",
            )

    @unittest.skipUnless(_POSIX, "file mode bits are POSIX specific")
    def test_log_file_and_directories_are_owner_only(self):
        log_path = self._temporary / "deep" / "job.log"
        with ExecutionLogWriter(log_path, secrets={"s3cret"}) as writer:
            writer.write_chunk(b"still alive\n")
        self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log_path.parent.stat().st_mode & 0o777, 0o700)

    # S3: the retention default and its clamped overrides.

    def test_default_retention_is_thirty_days_and_overrides_are_clamped(self):
        self.assertEqual(ExecutionLogCapture.DEFAULT_RETENTION_DAYS, 30)
        self.assertEqual(ExecutionLogCapture.retention_days(), 30)
        try:
            for raw, expected in (
                ("45", 45),
                ("0", 1),
                ("400", 365),
                ("garbage", 30),
                ("", 30),
                ("-5", 1),
            ):
                os.environ["WORKER_EXECUTION_LOG_RETENTION_DAYS"] = raw
                self.assertEqual(ExecutionLogCapture.retention_days(), expected, raw)
        finally:
            os.environ.pop("WORKER_EXECUTION_LOG_RETENTION_DAYS", None)
        custom = str(self._temporary / "custom-root")
        os.environ["WORKER_EXECUTION_LOG_ROOT"] = custom
        self.assertEqual(ExecutionLogCapture.default_root(), custom)
        os.environ.pop("WORKER_EXECUTION_LOG_ROOT", None)

    # Security invariant: raw unredacted runtime output never reaches the
    # host file (S5).

    def test_writer_redacts_secrets_before_anything_reaches_the_host_file(self):
        log_path = self._temporary / "job.log"
        writer = ExecutionLogWriter(log_path, secrets={"s3cret", "hunter2"})
        try:
            writer.write_chunk(b"step 1 token=s3cret ok\n")
            writer.write_event("progress", "detail hunter2 done")
        finally:
            writer.close()
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("s3cret", content)
        self.assertNotIn("hunter2", content)
        self.assertIn("step 1 token=<redacted> ok\n", content)
        self.assertIn("detail <redacted> done", content)
        self.assertIn("[started]", content)
        self.assertIn("[completed]", content)

    def test_writer_redacts_repository_urls_without_an_explicit_secret(self):
        log_path = self._temporary / "repo.log"
        writer = ExecutionLogWriter(log_path)
        try:
            writer.write_chunk(b"restic at https://user:pw@example.com/repo\n")
        finally:
            writer.close()
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("user:pw@example.com", content)
        self.assertIn("<redacted-repository>", content)

    def test_writer_redacts_utf8_secrets_split_across_byte_boundaries(self):
        secret = "s3cret-Ñoñ"
        raw = secret.encode("utf-8")
        log_path = self._temporary / "utf8-split.log"
        writer = ExecutionLogWriter(log_path, secrets={secret})
        try:
            writer.write_chunk(b"token=" + raw[:-1])
            writer.write_chunk(raw[-1:] + b" end\n")
        finally:
            writer.close()
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("s3cret", content)
        self.assertNotIn("\ufffd", content)
        self.assertIn("token=<redacted> end\n", content)

    # Long-URL split security (S6): a URL split across any chunk boundary
    # must never be persisted half-unredacted.

    def test_regression_url_chunks_split_at_every_point_stay_redacted(self):
        text = b"backup -r https://user:pw@example.com/repo done\n"
        expected_payload = "backup -r <redacted-repository> done\n"
        for split in range(1, len(text)):
            log_path = self._temporary / f"url-split-{split}.log"
            writer = ExecutionLogWriter(log_path)
            try:
                writer.write_chunk(text[:split])
                writer.write_chunk(text[split:])
            finally:
                writer.close()
            content = log_path.read_text(encoding="utf-8")
            self.assertNotIn("user:pw@example.com", content, split)
            self.assertEqual(expected_payload, _payload(content), split)

    def test_writer_holds_back_colon_schemes_split_across_chunks(self):
        log_path = self._temporary / "rclone-split.log"
        writer = ExecutionLogWriter(log_path)
        try:
            writer.write_chunk(b"config rclon")
            writer.write_chunk(b"e:user:pw@remote path\n")
        finally:
            writer.close()
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("user:pw@remote", content)
        self.assertIn("<redacted-repository>", content)

    def test_regression_secret_chunks_split_at_every_point_stay_redacted(self):
        text = b"launch token=S3cretPASS ok\n"
        for split in range(1, len(text)):
            log_path = self._temporary / f"secret-split-{split}.log"
            writer = ExecutionLogWriter(log_path, secrets={"S3cretPASS"})
            try:
                writer.write_chunk(text[:split])
                writer.write_chunk(text[split:])
            finally:
                writer.close()
            content = log_path.read_text(encoding="utf-8")
            self.assertNotIn("S3cretPASS", content, split)
            self.assertIn("token=<redacted> ok\n", content, split)

    def test_regression_url_and_secrets_across_multiple_chunks(self):
        log_path = self._temporary / "multi.log"
        writer = ExecutionLogWriter(log_path, secrets={"S3cretPASSxYz"})
        try:
            writer.write_chunk(b"job -r htt")
            writer.write_chunk(b"p://user")
            writer.write_chunk(b":pw@example.com/repo")
            writer.write_chunk(b" token=S3cre")
            writer.write_chunk(b"tPASSxYz ok\n")
        finally:
            writer.close()
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("user:pw@example.com", content)
        self.assertNotIn("S3cretPASSxYz", content)
        self.assertEqual(
            "job -r <redacted-repository> token=<redacted> ok\n", _payload(content)
        )

    # Parent-reproduced leak (S6): after an unterminated URL head is dropped
    # at the memory bound, continuation chunks stay discarded until the URL
    # terminator arrives.

    def test_writer_holds_back_url_heads_across_many_chunks(self):
        log_path = self._temporary / "url-many.log"
        writer = ExecutionLogWriter(log_path)
        try:
            writer.write_chunk(b"https://")
            writer.write_chunk(b"a" * (ExecutionLogWriter.MAX_PENDING_CHARS + 1))
            writer.write_chunk(b"credential@example.com/x\n")
        finally:
            writer.close()
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("credential@example.com", content)
        self.assertIn("[truncated] unterminated", content)
        self.assertIn("[completed]", content)

    def test_writer_suppresses_only_until_the_url_terminator(self):
        log_path = self._temporary / "url-suppress.log"
        writer = ExecutionLogWriter(log_path)
        try:
            writer.write_chunk(b"https://")
            writer.write_chunk(b"a" * (ExecutionLogWriter.MAX_PENDING_CHARS + 1))
            writer.write_chunk(b"credential@example.com/x after-term end\n")
            writer.write_chunk(b"more plain output\n")
        finally:
            writer.close()
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("credential@example.com", content)
        self.assertIn("after-term end\n", content)
        self.assertIn("more plain output\n", content)
        self.assertIn("[completed]", content)

    def test_writer_drops_unterminated_tokens_at_the_memory_bound_and_keeps_capturing(self):
        log_path = self._temporary / "overflow.log"
        writer = ExecutionLogWriter(log_path, secrets={"s3cret"})
        self.assertGreater(ExecutionLogWriter.MAX_PENDING_CHARS, 0)
        try:
            writer.write_chunk(b"https://" + b"t" * (ExecutionLogWriter.MAX_PENDING_CHARS + 1))
            writer.write_chunk(b"\n")
            writer.write_chunk(b"captured after overflow\n")
        finally:
            writer.close()
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("captured after overflow\n", content)
        self.assertIn("[truncated] unterminated", content)
        self.assertNotIn("t" * (ExecutionLogWriter.MAX_PENDING_CHARS + 1), content)

    # S5: bounded crash-resistant evidence.

    def test_writer_stays_bounded_and_records_truncation(self):
        log_path = self._temporary / "bounded.log"
        writer = ExecutionLogWriter(log_path, maximum_bytes=1500)
        try:
            writer.write_chunk(b"x" * 5000)
            writer.write_chunk(b"this must not be stored\n")
        finally:
            writer.close()
        self.assertLessEqual(log_path.stat().st_size, 1500)
        self.assertTrue(writer.truncated)
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("[truncated]", content)
        self.assertNotIn("this must not be stored", content)

    def test_writer_reports_interrupted_and_skips_writes_after_close(self):
        log_path = self._temporary / "interrupted.log"
        writer = ExecutionLogWriter(log_path)
        writer.write_chunk(b"partial evidence\n")
        writer.close()
        writer.write_chunk(b"after close\n")
        writer.close()
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("after close", content)

    # S4: capture failure cannot compromise normal execution.

    def test_capture_unavailable_when_log_directory_cannot_exist(self):
        blocker = self._temporary / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(ExecutionLogCaptureUnavailable):
            ExecutionLogWriter(blocker / "job.log")
        with self.assertRaises(ExecutionLogCaptureUnavailable):
            ExecutionLogCapture.open_writer(blocker, "worker-1", "docker", "job-1", when=_FIXED_WHEN)

    def test_open_writer_avoids_job_name_reuse_within_the_same_second(self):
        root = self._temporary / "logs"
        with ExecutionLogCapture.open_writer(root, "worker-1", "native", "job-1", when=_FIXED_WHEN) as first:
            first.write_chunk(b"attempt one\n")
        with ExecutionLogCapture.open_writer(root, "worker-1", "native", "job-1", when=_FIXED_WHEN) as second:
            second.write_chunk(b"attempt two\n")
        stamp = int(_FIXED_WHEN.timestamp())
        base = root / "worker-1" / "native" / "20260107" / f"job-1-{stamp}.log"
        alternative = root / "worker-1" / "native" / "20260107" / f"job-1-{stamp}-1.log"
        self.assertTrue(base.exists())
        self.assertTrue(alternative.exists())
        self.assertIn("attempt one\n", base.read_text(encoding="utf-8"))
        self.assertIn("attempt two\n", alternative.read_text(encoding="utf-8"))

    def test_detached_capture_child_redacts_and_completes_after_the_pipe_dies(self):
        log_path = self._temporary / "captured.log"
        recipe_path = self._temporary / "recipe.json"
        ExecutionLogCapture.write_redaction_recipe(recipe_path, ["s3cret", "hunter2"])
        child = ExecutionLogCapture.spawn_detached_writer(log_path, recipe_path, stdin=subprocess.PIPE)
        try:
            child.stdin.write(b"launch secret=s3cret\n")
            child.stdin.flush()
            child.stdin.write(b"hunter2 phase\n")
            child.stdin.flush()
            child.stdin.close()
            self.assertEqual(child.wait(timeout=30), 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("s3cret", content)
        self.assertNotIn("hunter2", content)
        self.assertIn("launch secret=<redacted>\n", content)
        self.assertIn("<redacted> phase\n", content)
        self.assertIn("[completed]", content)
        self.assertFalse(recipe_path.exists())

    def test_source_script_mode_child_runs_from_a_non_project_cwd(self):
        """Source-mode regression: the capture child also runs as a directly
        executed module script with a non-project cwd, where neither the
        ``src.*`` package nor relative imports resolve; the redactor must come
        from the sibling import fallback and still redact before persisting."""
        module = sys.modules["src.worker_agent.infrastructure.security.execution_log_capture"]
        script = os.path.abspath(module.__file__)
        log_path = self._temporary / "script-mode.log"
        recipe_path = self._temporary / "script-recipe.json"
        ExecutionLogCapture.write_redaction_recipe(recipe_path, ["s3cret-script"])
        completed = subprocess.run(
            [
                sys.executable, script, "--run-capture",
                "--capture", str(log_path), "--recipe", str(recipe_path),
            ],
            input=b"direct secret=s3cret-script line\nplain tail\n",
            cwd=str(self._temporary),
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("s3cret-script", content)
        self.assertIn("direct secret=<redacted> line\n", content)
        self.assertIn("plain tail\n", content)
        self.assertIn("[completed]", content)
        self.assertFalse(recipe_path.exists())

    def test_source_script_mode_child_fails_closed_without_a_valid_recipe(self):
        module = sys.modules["src.worker_agent.infrastructure.security.execution_log_capture"]
        script = os.path.abspath(module.__file__)
        recipe_path = self._temporary / "script-bad-recipe.json"
        recipe_path.write_text("not json", encoding="utf-8")
        log_path = self._temporary / "script-unprotected.log"
        completed = subprocess.run(
            [
                sys.executable, script, "--run-capture",
                "--capture", str(log_path), "--recipe", str(recipe_path),
            ],
            input=b"secret data s3cret-script\n",
            cwd=str(self._temporary),
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(2, completed.returncode)
        self.assertFalse(log_path.exists())

    def test_capture_child_never_persists_without_a_redaction_recipe(self):
        log_path = self._temporary / "unprotected.log"
        recipe_path = self._temporary / "corrupt-recipe.json"
        recipe_path.write_text("not json", encoding="utf-8")
        child = ExecutionLogCapture.spawn_detached_writer(log_path, recipe_path, stdin=subprocess.PIPE)
        try:
            child.stdin.write(b"secret data s3cret\n")
            child.stdin.flush()
            child.stdin.close()
            self.assertEqual(child.wait(timeout=30), 2)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        self.assertFalse(log_path.exists())

    # An empty recipe is valid for a job without secrets; recipe integrity
    # is what gates raw-output persistence.

    def test_empty_recipe_is_valid_and_child_only_writes_when_integrity_holds(self):
        recipe = self._temporary / "empty-recipe.json"
        ExecutionLogCapture.write_redaction_recipe(recipe, [])
        log_path = self._temporary / "empty.log"
        child = ExecutionLogCapture.spawn_detached_writer(log_path, recipe, stdin=subprocess.PIPE)
        try:
            child.stdin.write(b"public output only\n")
            child.stdin.flush()
            child.stdin.close()
            self.assertEqual(child.wait(timeout=30), 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("public output only\n", content)
        self.assertIn("[completed]", content)

    # Frozen dispatch: packaged workers re-invoke the same executable with
    # the non-public CLI sentinel dispatched by src.worker_agent.cli exactly
    # like the internal entrypoints; source runs keep the module script
    # entrypoint. Only paths and fixed options travel on argv; secrets travel
    # in the recipe file, never on the command line.

    def test_capture_child_command_covers_source_frozen_and_docker_modes(self):
        sentinel = ExecutionLogCapture.CAPTURE_CHILD_CLI_SENTINEL
        module = sys.modules["src.worker_agent.infrastructure.security.execution_log_capture"]
        self.assertEqual(
            ExecutionLogCapture.capture_child_command("l.log", "r.json"),
            [sys.executable, os.path.abspath(module.__file__), "--run-capture",
             "--capture", "l.log", "--recipe", "r.json"],
        )
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(
                ExecutionLogCapture.capture_child_command("log.log", "recipe.json"),
                [sys.executable, sentinel, "--capture", "log.log", "--recipe", "recipe.json"],
            )
            self.assertEqual(
                ExecutionLogCapture.capture_child_command(
                    "log.log", "recipe.json", container_id="fakecontainer", socket_path="fake.sock"
                ),
                [sys.executable, sentinel,
                 "--capture", "log.log", "--recipe", "recipe.json",
                 "--container", "fakecontainer", "--socket", "fake.sock"],
            )
        with self.assertRaises(ValueError):
            ExecutionLogCapture.capture_child_command("l.log", "r.json", container_id="../escape")

    def test_spawn_detached_launches_children_detached_with_the_right_entrypoint(self):
        sentinel = ExecutionLogCapture.CAPTURE_CHILD_CLI_SENTINEL
        module = sys.modules["src.worker_agent.infrastructure.security.execution_log_capture"]
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch(
            "src.worker_agent.infrastructure.security.execution_log_capture.subprocess.Popen"
        ) as popen:
            ExecutionLogCapture.spawn_detached_writer("log.log", "recipe.json", stdin="read-end")
        arguments, keyword_arguments = popen.call_args
        self.assertEqual(
            arguments[0],
            [sys.executable, sentinel, "--capture", "log.log", "--recipe", "recipe.json"],
        )
        self.assertEqual(keyword_arguments.get("stdin"), "read-end")
        self._assert_detached_flags(keyword_arguments)
        with mock.patch(
            "src.worker_agent.infrastructure.security.execution_log_capture.subprocess.Popen"
        ) as popen:
            ExecutionLogCapture.spawn_detached_docker_capture(
                "log.log", "recipe.json", "fakecontainer", socket_path="fake.sock"
            )
        arguments, keyword_arguments = popen.call_args
        self.assertEqual(
            arguments[0],
            [sys.executable, os.path.abspath(module.__file__), "--run-capture",
             "--capture", "log.log", "--recipe", "recipe.json",
             "--container", "fakecontainer", "--socket", "fake.sock"],
        )
        self.assertEqual(keyword_arguments.get("stdin"), subprocess.DEVNULL)
        self._assert_detached_flags(keyword_arguments)

    def _assert_detached_flags(self, keyword_arguments):
        self.assertEqual(keyword_arguments.get("stdout"), subprocess.DEVNULL)
        self.assertEqual(keyword_arguments.get("stderr"), subprocess.DEVNULL)
        if os.name == "nt":
            self.assertEqual(
                keyword_arguments.get("creationflags"),
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        else:
            self.assertTrue(keyword_arguments.get("start_new_session"))

    # CLI compatibility: the packaged CLI internal entrypoint must surface
    # the capture child's exit codes as the child process exit status.

    def test_worker_cli_entrypoint_dispatches_the_capture_child_with_exact_exit_status(self):
        from src.worker_agent import cli

        log_path = self._temporary / "dispatched.log"
        recipe_path = self._temporary / "dispatch-recipe.json"
        ExecutionLogCapture.write_redaction_recipe(recipe_path, ["s3cret"])
        stdin = io.TextIOWrapper(io.BytesIO(b"dispatch secret=s3cret\n"))
        with mock.patch("sys.stdin", stdin):
            self.assertEqual(
                cli.main([ExecutionLogCapture.CAPTURE_CHILD_CLI_SENTINEL,
                          "--capture", str(log_path), "--recipe", str(recipe_path)]),
                0,
            )
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("s3cret", content)
        self.assertIn("dispatch secret=<redacted>\n", content)
        self.assertIn("[completed]", content)
        self.assertFalse(recipe_path.exists())

        failed_recipe = self._temporary / "dispatch-bad-recipe.json"
        failed_recipe.write_text("not json", encoding="utf-8")
        stdin = io.TextIOWrapper(io.BytesIO(b"secret data s3cret\n"))
        with mock.patch("sys.stdin", stdin):
            self.assertEqual(
                cli.main([ExecutionLogCapture.CAPTURE_CHILD_CLI_SENTINEL,
                          "--capture", str(log_path), "--recipe", str(failed_recipe)]),
                2,
            )
        self.assertFalse((self._temporary / "dispatch-failure.log").exists())

    @unittest.skipUnless(_POSIX, "deleting a read-only file is POSIX behavior")
    def test_redaction_recipe_round_trip_bounds_and_deletion(self):
        recipe = self._temporary / "recipe.json"
        ExecutionLogCapture.write_redaction_recipe(recipe, ["s3cret", "", 5, "hunter2", "s3cret"])
        self.assertEqual(recipe.stat().st_mode & 0o777, 0o600)
        values = ExecutionLogCapture.load_redaction_recipe(recipe)
        self.assertEqual(values, ["s3cret", "hunter2"])
        self.assertFalse(recipe.exists())
        with self.assertRaises(ValueError):
            ExecutionLogCapture.write_redaction_recipe(recipe, [f"s{index}" for index in range(600)])
        with self.assertRaises(ValueError):
            ExecutionLogCapture.write_redaction_recipe(
                recipe, ["x" * (ExecutionLogCapture.MAX_RECIPE_BYTES + 1)]
            )

    @unittest.skipUnless(_POSIX, "umask behavior is POSIX specific")
    def test_recipe_created_owner_only_even_with_a_permissive_umask(self):
        recipe = self._temporary / "recipe-umask.json"
        previous = os.umask(0)
        try:
            ExecutionLogCapture.write_redaction_recipe(recipe, ["s3cret"])
        finally:
            os.umask(previous)
        self.assertEqual(recipe.stat().st_mode & 0o777, 0o600)

    def test_recipe_collision_raises_file_exists_and_leaves_preexisting_untouched(self):
        recipe = self._temporary / "recipe-collision.json"
        recipe.write_bytes(b"pre-existing content")
        with self.assertRaises(FileExistsError):
            ExecutionLogCapture.write_redaction_recipe(recipe, ["s3cret"])
        self.assertEqual(b"pre-existing content", recipe.read_bytes())

    # S6: Windows ACL-before-write hardening.

    @unittest.skipUnless(os.name == "nt", "Windows ACL hardening")
    def test_windows_log_file_acl_is_secured_before_content_is_written(self):
        log_path = self._temporary / "acl-file.log"
        writer = ExecutionLogWriter(log_path, secrets={"s3cret"})
        try:
            writer.write_chunk(b"token=s3cret\n")
        finally:
            writer.close()
        listing = subprocess.run(
            ["icacls", str(log_path)], check=True, capture_output=True, text=True
        ).stdout
        self.assertNotIn("(I)", listing)
        expected_account = subprocess.run(
            ["whoami"], check=True, capture_output=True, text=True
        ).stdout.strip().casefold()
        self.assertIn(expected_account, listing.casefold())
        self.assertIn("SYSTEM", listing)

    @unittest.skipUnless(os.name == "nt", "Windows ACL hardening")
    def test_windows_recipe_acl_is_explicit_and_not_inherited(self):
        recipe = self._temporary / "recipe-acl.json"
        ExecutionLogCapture.write_redaction_recipe(recipe, ["s3cret"])
        listing = subprocess.run(
            ["icacls", str(recipe)], check=True, capture_output=True, text=True
        ).stdout
        self.assertNotIn("(I)", listing)
        expected_account = subprocess.run(
            ["whoami"], check=True, capture_output=True, text=True
        ).stdout.strip().casefold()
        self.assertIn(expected_account, listing.casefold())
        self.assertIn("SYSTEM", listing)

    @unittest.skipUnless(os.name == "nt", "Windows ACL hardening")
    def test_recipe_hardens_acl_before_secret_bytes_are_written(self):
        recipe = self._temporary / "recipe-order.json"
        observations = []
        original = ExecutionLogCapture._restrict_windows_acl

        def observed_acl(path):
            size = os.stat(path).st_size
            try:
                content = Path(path).read_bytes()
            except OSError:
                content = b"<unreadable>"
            observations.append((size, content))
            original(path)

        with mock.patch.object(ExecutionLogCapture, "_restrict_windows_acl", observed_acl):
            ExecutionLogCapture.write_redaction_recipe(recipe, ["s3cret"])
        self.assertEqual(1, len(observations))
        hardened_size, hardened_content = observations[0]
        self.assertEqual(0, hardened_size)
        self.assertEqual(b"", hardened_content)
        self.assertEqual(b'["s3cret"]', recipe.read_bytes())

    # Facade regression: recipe creation is delegated to the focused sibling
    # recipe component, but this class's historical ``_restrict_windows_acl``
    # mock seam must remain the ACL hook actually invoked (cross-platform
    # counterpart of the Windows ordering and hardening-failure regressions
    # around this test).

    def test_recipe_acl_mock_seam_remains_the_invoked_hook(self):
        recipe = self._temporary / "recipe-seam.json"
        observed = []
        with mock.patch.object(ExecutionLogCapture, "_restrict_windows_acl", observed.append):
            ExecutionLogCapture.write_redaction_recipe(recipe, ["s3cret"])
        self.assertEqual([recipe], observed)
        self.assertEqual(b'["s3cret"]', recipe.read_bytes())

        def failing_acl(path):
            raise ExecutionLogCaptureUnavailable(f"simulated hardening failure: {path}")

        failed_recipe = self._temporary / "recipe-seam-failed.json"
        with mock.patch.object(ExecutionLogCapture, "_restrict_windows_acl", failing_acl):
            with self.assertRaises(ExecutionLogCaptureUnavailable):
                ExecutionLogCapture.write_redaction_recipe(failed_recipe, ["s3cret"])
        self.assertFalse(failed_recipe.exists())  # hook failure still deletes fail-closed

    # S3: retention.

    def test_prune_enforces_retention_and_collapses_empty_directories(self):
        root = self._temporary / "logs"
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(days=35)).timestamp()
        fresh_time = (now - timedelta(days=2)).timestamp()
        stale_log = root / "worker-a" / "docker" / "20200101" / "job-old-1700000000.log"
        fresh_log = root / "worker-b" / "native" / "20200102" / "job-fresh-1700000001.log"
        unrelated = root / "worker-a" / "docker" / "old-job.txt"
        for path, stamp in ((stale_log, old_time), (fresh_log, fresh_time), (unrelated, old_time)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("evidence", encoding="utf-8")
            os.utime(path, (stamp, stamp))
        summary = ExecutionLogCapture.prune(root, now=now, retention_days=30)
        self.assertEqual(summary["retention_days"], 30)
        self.assertEqual(summary["inspected"], 2)
        self.assertEqual(summary["deleted"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertFalse(stale_log.exists())
        self.assertTrue(fresh_log.exists())
        self.assertTrue(unrelated.exists())
        self.assertFalse(stale_log.parent.exists())
        self.assertTrue((root / "worker-a").exists())
        self.assertTrue((root / "worker-b" / "native").exists())

    def test_prune_fails_open_on_a_missing_root(self):
        summary = ExecutionLogCapture.prune(self._temporary / "absent-root")
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["retention_days"], ExecutionLogCapture.DEFAULT_RETENTION_DAYS)

    @unittest.skipUnless(_POSIX, "unlink failure requires a read-only directory")
    def test_prune_counts_failures_without_raising(self):
        root = self._temporary / "logs"
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(days=35)).timestamp()
        locked = root / "locked" / "docker" / "20200101"
        locked.mkdir(parents=True)
        stale = locked / "job-stale-1234567890.log"
        stale.write_text("evidence", encoding="utf-8")
        os.utime(stale, (old_time, old_time))
        os.chmod(root / "locked" / "docker" / "20200101", 0o555)
        try:
            summary = ExecutionLogCapture.prune(root, now=now, retention_days=30)
        finally:
            os.chmod(root / "locked" / "docker" / "20200101", 0o755)
        self.assertEqual(summary["deleted"], 0)
        self.assertEqual(summary["failed"], 1)
        self.assertTrue(stale.exists())

    def test_prune_never_removes_files_outside_the_managed_log_layout(self):
        root = self._temporary / "logs"
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(days=35)).timestamp()
        managed = root / "worker-a" / "docker" / "20200101" / "job-1234567890.log"
        managed_suffix = root / "worker-a" / "native" / "20200102" / "job-1234567890-1.log"
        unmanaged_name = root / "worker-a" / "docker" / "20200101" / "app.log"
        stale_but_unshaped = root / "worker-a" / "docker" / "20200101" / "2020010112.log"
        shallow = root / "system.log"
        nested_unmanaged = root / "other" / "nested" / "service.log"
        empty_unmanaged = root / "other" / "empty-dir"
        paths = (managed, managed_suffix, unmanaged_name, stale_but_unshaped, shallow, nested_unmanaged)
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("evidence", encoding="utf-8")
            os.utime(path, (old_time, old_time))
        empty_unmanaged.mkdir()
        summary = ExecutionLogCapture.prune(root, now=now, retention_days=30)
        self.assertEqual(summary["deleted"], 2)
        self.assertEqual(summary["inspected"], 2)
        self.assertFalse(managed.exists())
        self.assertFalse(managed_suffix.exists())
        for kept in (unmanaged_name, stale_but_unshaped, shallow, nested_unmanaged):
            self.assertTrue(kept.exists(), kept)
        self.assertTrue(empty_unmanaged.exists())
        self.assertTrue(unmanaged_name.parent.exists())
        self.assertFalse(managed_suffix.parent.exists())
        self.assertFalse((root / "worker-a" / "native").exists())

    # S3: retention must recognize every log path the fixed structure
    # generates. Job IDs are accepted up to the component grammar maximum of
    # 128 characters, so stale evidence named with such an ID (including the
    # first excluded length, 102) must still be reclaimed: pruning only the
    # shorter names would let evidence survive retention forever.

    def test_prune_reclaims_maximum_length_job_id_logs_the_structure_generated(self):
        root = self._temporary / "logs"
        now = datetime.now(timezone.utc)
        for position, job_id in enumerate(("job-" + "a" * 101, "job-" + "a" * 124)):  # 102 and 128 characters
            with self.subTest(job_id=job_id, length=len(job_id)):
                with ExecutionLogCapture.open_writer(
                    root, "worker-1", "docker", job_id, when=now
                ) as stale:
                    stale.write_chunk(b"stale evidence\n")
                generated = ExecutionLogCapture.job_log_path(
                    root, "worker-1", "docker", job_id, when=now
                )
                self.assertTrue(generated.exists())
                aged = (now - timedelta(days=40)).timestamp()
                os.utime(generated, (aged, aged))
                summary = ExecutionLogCapture.prune(root, now=now, retention_days=30)
                self.assertEqual(summary["inspected"], 1)
                self.assertEqual(summary["deleted"], 1)
                self.assertEqual(summary["failed"], 0)
                self.assertFalse(generated.exists())


class _FakeSDKStream:
    """Fake of the Docker SDK log stream: yields demultiplexed chunks and
    supports ``close()`` so cleanup behavior can be asserted."""

    def __init__(self, chunks=(), *, error=None):
        self._chunks = list(chunks)
        self._error = error
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._chunks:
            return self._chunks.pop(0)
        if self._error is not None:
            raise self._error
        raise StopIteration

    def close(self):
        self.closed = True


class _FakeSDKContainer:
    """Fake SDK container exposing only the read-only ``logs`` method."""

    def __init__(self, *, chunks=(), logs_error=None, stream=None):
        self.logs_calls = []
        self._chunks = list(chunks)
        self._logs_error = logs_error
        self._stream = stream

    def logs(self, **kwargs):
        self.logs_calls.append(kwargs)
        if self._logs_error is not None:
            raise self._logs_error
        return self._stream if self._stream is not None else iter(tuple(self._chunks))


class _FakeSDKContainers:
    """Fake SDK containers collection exposing only ``get``."""

    def __init__(self, container, *, error=None):
        self._container = container
        self._error = error
        self.get_calls = []

    def get(self, container_id):
        self.get_calls.append(container_id)
        if self._error is not None:
            raise self._error
        return self._container


class _FakeSDKClient:
    """Fake SDK client: only the read-only containers resolution exists.

    The fakes only expose ``get`` and ``logs``, so any modifying SDK call
    (remove/stop/kill/restart/pause/wait/exec) fails the test by construction.
    """

    def __init__(self, containers):
        self.containers = containers


class _SeamSource:
    """Fake of the ``DockerContainerLogSource`` seam the child consumes."""

    def __init__(self, stream):
        self._stream = stream

    def open_stream(self):
        return self._stream


class _UnavailableSource:
    def open_stream(self):
        raise ExecutionLogCaptureUnavailable("daemon socket is unreachable")


def _docker_argv(log_path, recipe_path, container_id):
    return [
        "--capture", str(log_path),
        "--recipe", str(recipe_path),
        "--container", container_id,
    ]


class DockerContainerLogSourceTests(unittest.TestCase):
    """Docker log source through the project's Docker SDK (fake SDK seam).

    Must use exactly the supported logs API already used by
    ``DockerRuntimeAdapter`` (S2) and map SDK failures onto
    ``ExecutionLogCaptureUnavailable``; the strict fakes expose only the two
    read-only SDK calls the child needs, so any modifying SDK call fails the
    test by construction (S6).
    """

    CONTAINER = "fakecontainer"

    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-capture-"))

    def tearDown(self):
        shutil.rmtree(self._temporary, ignore_errors=True)

    def _nonexistent_socket(self):
        return (
            r"\\.\pipe\docker_engine_capture_missing"
            if os.name == "nt"
            else str(self._temporary / "missing.sock")
        )

    def _source(self, client):
        base_urls = []

        def client_factory(base_url, *args, **keyword_arguments):
            base_urls.append(base_url)
            return client

        source = DockerContainerLogSource(
            self.CONTAINER, socket_path=self._nonexistent_socket(), client_factory=client_factory
        )
        return source, base_urls

    def _skip_without_docker_sdk(self):
        if not _DOCKER_SDK:
            raise unittest.SkipTest("docker SDK not importable")

    def test_source_uses_the_supported_sdk_logs_api(self):
        container = _FakeSDKContainer(chunks=(b"out line\n", b"err line\n"))
        source, _ = self._source(_FakeSDKClient(_FakeSDKContainers(container)))
        self.assertEqual([b"out line\n", b"err line\n"], list(source.open_stream()))
        self.assertEqual(1, len(container.logs_calls))
        self.assertEqual(
            {"stdout": True, "stderr": True, "stream": True, "follow": True},
            container.logs_calls[0],
        )

    def test_source_maps_sdk_open_failures_to_the_unavailable_capture_error(self):
        cases = (
            _FakeSDKClient(_FakeSDKContainers(_FakeSDKContainer(), error=RuntimeError("no such container"))),
            _FakeSDKClient(_FakeSDKContainers(_FakeSDKContainer(logs_error=RuntimeError("api error")))),
        )
        for position, client in enumerate(cases):
            with self.subTest(case=position):
                source, _ = self._source(client)
                with self.assertRaises(ExecutionLogCaptureUnavailable):
                    source.open_stream()

    # S6: the daemon URL translation must survive the project SDK's own URL
    # parser (docker-py 7.x ``parse_host``): it must be normalized to exactly
    # the URL forms the SDK uses for itself, without contacting a daemon.

    _SDK_WINDOWS_PIPE_URL = "npipe:////./pipe/docker_engine"
    _SDK_UNIX_PARSED_URL = "http+unix:///var/run/docker.sock"

    def test_socket_base_url_round_trips_windows_pipe_through_sdk_parse_host(self):
        self._skip_without_docker_sdk()
        for raw in (r"\\.\pipe\docker_engine", "//./pipe/docker_engine"):
            with self.subTest(socket=raw):
                url = DockerContainerLogSource.socket_base_url(raw)
                self.assertEqual(self._SDK_WINDOWS_PIPE_URL, docker.utils.parse_host(url))

    def test_socket_base_url_round_trips_unix_socket_through_sdk_parse_host(self):
        self._skip_without_docker_sdk()
        url = DockerContainerLogSource.socket_base_url("/var/run/docker.sock")
        self.assertEqual("unix:///var/run/docker.sock", url)
        self.assertEqual(self._SDK_UNIX_PARSED_URL, docker.utils.parse_host(url))
        custom = DockerContainerLogSource.socket_base_url("/var/run/custom.sock")
        self.assertEqual("http+unix:///var/run/custom.sock", docker.utils.parse_host(custom))

    def test_default_socket_translates_to_the_sdk_daemon_default(self):
        self._skip_without_docker_sdk()
        url = DockerContainerLogSource.socket_base_url(
            DockerContainerLogSource.default_socket()
        )
        if os.name == "nt":
            self.assertEqual(self._SDK_WINDOWS_PIPE_URL, docker.utils.parse_host(url))
        else:
            self.assertEqual(self._SDK_UNIX_PARSED_URL, docker.utils.parse_host(url))

    def test_translated_url_is_accepted_by_the_real_sdk_client_builder(self):
        self._skip_without_docker_sdk()
        # Pinned API version keeps APIClient construction offline.
        pipe_client = docker.APIClient(
            base_url=DockerContainerLogSource.socket_base_url(r"\\.\pipe\docker_engine"),
            version=docker.constants.DEFAULT_DOCKER_API_VERSION,
        )
        self.assertEqual("http+docker://localnpipe", pipe_client.base_url)
        unix_client = docker.APIClient(
            base_url=DockerContainerLogSource.socket_base_url("/var/run/docker.sock"),
            version=docker.constants.DEFAULT_DOCKER_API_VERSION,
        )
        self.assertEqual("http+docker://localhost", unix_client.base_url)

    def test_socket_base_url_rejects_unsupported_locations(self):
        unsupported = (
            "",
            "   ",
            "container.sock",
            "relative/path.sock",
            "tcp://localhost:2375",
            "npipe:////./pipe/docker_engine",
            "unix:///var/run/docker.sock",
            "//./pipe/",
            "//./pipe//",
            "//./pipe/with spaces",
            "//./pipe/../escape",
            None,
        )
        for raw in unsupported:
            with self.subTest(socket=raw):
                with self.assertRaises(ValueError):
                    DockerContainerLogSource.socket_base_url(raw)

    def test_source_closes_the_sdk_stream_after_completion_and_after_errors(self):
        completed = _FakeSDKStream([b"done\n"])
        source, _ = self._source(_FakeSDKClient(_FakeSDKContainers(_FakeSDKContainer(stream=completed))))
        list(source.open_stream())
        self.assertTrue(completed.closed)

        broken = _FakeSDKStream([b"partial evidence\n"], error=OSError("broken stream"))
        source, _ = self._source(_FakeSDKClient(_FakeSDKContainers(_FakeSDKContainer(stream=broken))))
        consumed = []
        with self.assertRaises(OSError):
            for chunk in source.open_stream():
                consumed.append(chunk)
        self.assertEqual([b"partial evidence\n"], consumed)
        self.assertTrue(broken.closed)

    def test_source_rejects_unsafe_container_identifiers(self):
        unsafe_values = [
            "", " ", "../escape", "with/slash", "a?query=1", "a HTTP/1.1",
            "..", "-leading", ".hidden", "id&rm=1", "a#fragment",
        ]
        for value in unsafe_values:
            with self.subTest(container=value):
                with self.assertRaises(ValueError):
                    DockerContainerLogSource(value)

    def test_source_fails_closed_when_the_docker_sdk_is_missing(self):
        module = sys.modules["src.worker_agent.infrastructure.security.execution_log_capture"]
        source = DockerContainerLogSource(self.CONTAINER, socket_path=self._nonexistent_socket())
        with mock.patch.object(module, "docker", None):
            with self.assertRaises(ExecutionLogCaptureUnavailable):
                source.open_stream()


class ExecutionLogCaptureDockerChildTests(unittest.TestCase):
    """Docker capture-child mode: the detached child reads the SDK log follow
    stream, redacts every chunk before persistence and survives the worker
    disappearing (S2, S5). Strict fakes only (no Docker daemon), plus one
    real child process against an unreachable daemon socket (S6).
    """

    CONTAINER = "fakecontainer"

    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-capture-"))

    def tearDown(self):
        shutil.rmtree(self._temporary, ignore_errors=True)

    def test_docker_capture_child_streams_and_redacts_daemon_logs(self):
        log_path = self._temporary / "docker-capture.log"
        recipe_path = self._temporary / "recipe.json"
        ExecutionLogCapture.write_redaction_recipe(recipe_path, ["s3cret", "hunter2"])
        source = _SeamSource(_FakeSDKStream([
            b"backup token=s3cret stream\n",
            b"error secret=hunter2\n",
        ]))
        exit_code = run_capture_child(
            _docker_argv(log_path, recipe_path, self.CONTAINER), docker_source=source
        )
        self.assertEqual(0, exit_code)
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("s3cret", content)
        self.assertNotIn("hunter2", content)
        self.assertIn("backup token=<redacted> stream\n", content)
        self.assertIn("error secret=<redacted>\n", content)
        self.assertIn("[completed]", content)
        self.assertFalse(recipe_path.exists())

    def test_docker_capture_child_fails_safe_when_the_daemon_cannot_be_reached(self):
        log_path = self._temporary / "unreachable.log"
        recipe_path = self._temporary / "recipe.json"
        ExecutionLogCapture.write_redaction_recipe(recipe_path, ["s3cret"])
        exit_code = run_capture_child(
            _docker_argv(log_path, recipe_path, self.CONTAINER), docker_source=_UnavailableSource()
        )
        self.assertEqual(CAPTURE_CHILD_EXIT_DAEMON_FAILURE, exit_code)
        self.assertFalse(log_path.exists())

    def test_docker_capture_child_returns_recipe_failure_for_invalid_identifiers(self):
        for position, value in enumerate(["", "../escape", "with/slash", "a?query=1", "a b"]):
            with self.subTest(container=value):
                log_path = self._temporary / f"invalid-{position}.log"
                recipe_path = self._temporary / f"invalid-recipe-{position}.json"
                ExecutionLogCapture.write_redaction_recipe(recipe_path, ["s3cret"])
                exit_code = run_capture_child(_docker_argv(log_path, recipe_path, value))
                self.assertEqual(2, exit_code)
                self.assertFalse(log_path.exists())

    def test_docker_capture_child_records_interrupted_status_when_the_stream_breaks(self):
        log_path = self._temporary / "broken-stream.log"
        recipe_path = self._temporary / "recipe.json"
        ExecutionLogCapture.write_redaction_recipe(recipe_path, ["s3cret"])
        source = _SeamSource(_FakeSDKStream([b"first token=s3cret\n"], error=OSError("broken stream")))
        exit_code = run_capture_child(
            _docker_argv(log_path, recipe_path, self.CONTAINER), docker_source=source
        )
        self.assertEqual(1, exit_code)
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("s3cret", content)
        self.assertIn("first token=<redacted>\n", content)
        self.assertIn("[interrupted]", content)

    def test_docker_option_parsing_rejects_duplicates_and_malformed_arguments(self):
        log_path = self._temporary / "argument-failure.log"
        recipe_path = self._temporary / "argument-recipe.json"
        ExecutionLogCapture.write_redaction_recipe(recipe_path, ["s3cret"])
        base = ["--capture", str(log_path), "--recipe", str(recipe_path)]
        for extra in (
            ["--container", "a", "--container", "b"],
            ["--socket", "x", "--socket", "y"],
            ["--container"],
            ["junk"],
            ["--container", ""],
        ):
            with self.subTest(argv=extra):
                exit_code = run_capture_child(base + extra)
                self.assertEqual(2, exit_code)
                self.assertFalse(log_path.exists())

    def test_stdin_mode_is_unchanged_when_no_container_is_requested(self):
        log_path = self._temporary / "stdin-regression.log"
        recipe_path = self._temporary / "stdin-recipe.json"
        ExecutionLogCapture.write_redaction_recipe(recipe_path, ["s3cret"])
        stream = io.TextIOWrapper(io.BytesIO(b"pipe token=s3cret text\n"))
        with mock.patch("sys.stdin", stream):
            exit_code = run_capture_child(
                ["--capture", str(log_path), "--recipe", str(recipe_path)]
            )
        self.assertEqual(0, exit_code)
        content = log_path.read_text(encoding="utf-8")
        self.assertNotIn("s3cret", content)
        self.assertIn("pipe token=<redacted> text\n", content)
        self.assertIn("[completed]", content)
        self.assertFalse(recipe_path.exists())

    def test_detached_docker_capture_child_fails_safe_without_a_daemon(self):
        socket_path = (
            r"\\.\pipe\docker_engine_capture_missing"
            if os.name == "nt"
            else str(self._temporary / "missing.sock")
        )
        log_path = self._temporary / "missing-daemon.log"
        recipe_path = self._temporary / "missing-daemon-recipe.json"
        ExecutionLogCapture.write_redaction_recipe(recipe_path, ["s3cret"])
        child = ExecutionLogCapture.spawn_detached_docker_capture(
            log_path, recipe_path, self.CONTAINER, socket_path=socket_path
        )
        try:
            self.assertEqual(CAPTURE_CHILD_EXIT_DAEMON_FAILURE, child.wait(timeout=60))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        self.assertFalse(log_path.exists())
        self.assertFalse(recipe_path.exists())


class ExecutorOwnedRelayTests(unittest.TestCase):
    """S5 executor-owning relay (native): a relay child owns the executor's
    own stdout/stderr pipes, forwards them verbatim to the worker (live
    progress is byte-identical) and feeds the detached capture child, so the
    capture continues after the worker or its live pipe dies, relay broken
    pipes or capture loss never kill or stall the executor, and the executor's
    exit code is propagated to the job result. "--" must carry the executor
    argv; secrets never belong in argv."""

    SECRET = "p3pp3r-relay-secret"

    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-relay-"))

    def tearDown(self):
        shutil.rmtree(self._temporary, ignore_errors=True)

    def _executor_script(self, lines=6, exit_code=0, interval=0.0):
        return (
            "import sys, time\n"
            "for i in range(%d):\n"
            "    print('tick-%%d %s' %% i, flush=True)\n"
            "    sys.stderr.write('err-%%d %s\\n' %% i); sys.stderr.flush()\n"
            "    time.sleep(%r)\n"
            "sys.exit(%d)\n"
        ) % (lines, self.SECRET, self.SECRET, interval, exit_code)

    def test_relay_command_keeps_argv_secret_free_and_is_dispatchable(self):
        log_path = "log"
        recipe_path = "recipe"
        real_argv = [sys.executable, "-c", "print('hi')"]
        command = ExecutionLogCapture.capture_relay_command(log_path, recipe_path, real_argv)
        self.assertEqual(
            command,
            [sys.executable, _CAPTURE_SCRIPT, "--run-relay", "--capture", log_path, "--recipe", recipe_path, "--", *real_argv],
        )
        self.assertNotIn(self.SECRET, str(command))
        # Malformed executor argv is rejected before anything spawns.
        for bad in (None, "not-a-list", [], ["python", 3]):
            with self.assertRaises((TypeError, ValueError)):
                ExecutionLogCapture.capture_relay_command(log_path, recipe_path, bad)
        # The frozen form re-invokes the packaged executable with the sentinel.
        with mock.patch.object(sys, "frozen", True, create=True):
            frozen_command = ExecutionLogCapture.capture_relay_command(log_path, recipe_path, real_argv)
        self.assertEqual(
            frozen_command,
            [sys.executable, ExecutionLogCapture.CAPTURE_CHILD_CLI_SENTINEL, "--run-relay", "--capture", log_path, "--recipe", recipe_path, "--", *real_argv],
        )

    def test_run_capture_child_relay_forwards_lives_streams_and_propagates_exit_code(self):
        log_path = str(self._temporary / "relay-e2e.log")
        recipe_path = str(self._temporary / "relay-e2e.recipe.json")
        ExecutionLogCapture.write_redaction_recipe(recipe_path, [self.SECRET])
        executor_argv = [sys.executable, "-c", self._executor_script(lines=3, exit_code=3)]
        relay_argv = [sys.executable, _CAPTURE_SCRIPT, "--run-capture", "--run-relay", "--capture", log_path, "--recipe", recipe_path, "--", *executor_argv]
        completed = subprocess.run(
            relay_argv,
            capture_output=True,
            timeout=120,
        )
        # Relay exit code == executor exit code (job semantics unchanged).
        self.assertEqual(completed.returncode, 3, completed.stderr)
        # Live stdout/stderr forwarded verbatim.
        self.assertEqual(
            # The relay forwards the executor raw bytes verbatim (raw CRLF
            # on Windows); the worker keeps its own universal-newline
            # reading and redaction, exactly as before the relay.
            completed.stdout.decode("utf-8").replace("\r\n", "\n"),
            "".join(f"tick-{i} {self.SECRET}\n" for i in range(3)),
        )
        self.assertEqual(
            completed.stderr.decode("utf-8").replace("\r\n", "\n"),
            "".join(f"err-{i} {self.SECRET}\n" for i in range(3)),
        )
        content = self._wait_for_log(log_path, ["[completed]"])
        self.assertNotIn(self.SECRET, content)
        self.assertIn("tick-2 <redacted>", content)
        self.assertFalse(Path(recipe_path).exists())

    def test_relay_keeps_capturing_after_the_live_reader_dies(self):
        """The worker's death (closing the live pipe) must neither kill the
        executor nor stop the capture: output produced afterwards is still
        persisted redacted."""
        log_path = str(self._temporary / "relay-death.log")
        recipe_path = str(self._temporary / "relay-death.recipe.json")
        ExecutionLogCapture.write_redaction_recipe(recipe_path, [self.SECRET])
        executor_argv = [sys.executable, "-c", self._executor_script(lines=12, exit_code=0, interval=0.05)]
        relay_argv = [sys.executable, _CAPTURE_SCRIPT, "--run-capture", "--run-relay", "--capture", log_path, "--recipe", recipe_path, "--", *executor_argv]
        relay = subprocess.Popen(relay_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        observed = ""
        deadline = __import__("time").monotonic() + 20
        while observed.count("tick-") < 2 and __import__("time").monotonic() < deadline:
            line = relay.stdout.readline()
            if not line:
                break
            observed += line.decode("utf-8")
        relay.stdout.close()  # abrupt "worker" death: the live reader is gone
        relay.stderr.close()
        code = relay.wait(timeout=30)
        self.assertEqual(code, 0, "the relay must exit with the executor code after its live reader died")
        content = self._wait_for_log(log_path, ["tick-11 <redacted>", "[completed]"])
        self.assertNotIn(self.SECRET, content)
        self.assertFalse(Path(recipe_path).exists())

    def _wait_for_log(self, log_path, markers, timeout=30):
        """The detached capture child finalizes asynchronously after the
        relay exits; bounded polling keeps the checks deterministic."""
        import time as _time
        deadline = _time.monotonic() + timeout
        content = ""
        while _time.monotonic() < deadline:
            try:
                content = Path(log_path).read_text(encoding="utf-8")
            except (FileNotFoundError, OSError):
                _time.sleep(0.1)
                continue
            if all(marker in content for marker in markers):
                return content
            _time.sleep(0.1)
        raise AssertionError(f"log {log_path} never reached markers {markers}; last: {content[:400]!r}")

    def test_relay_continues_the_executor_when_the_capture_child_dies(self):
        corrupt_recipe_path = str(self._temporary / "relay-corrupt.recipe.json")
        Path(corrupt_recipe_path).write_text("corrupt", encoding="utf-8")
        log_path = str(self._temporary / "relay-corrupt.log")
        executor_argv = [sys.executable, "-c", self._executor_script(lines=3, exit_code=7)]
        relay_argv = [sys.executable, _CAPTURE_SCRIPT, "--run-capture", "--run-relay", "--capture", log_path, "--recipe", corrupt_recipe_path, "--", *executor_argv]
        completed = subprocess.run(relay_argv, capture_output=True, timeout=120)
        # Capture failure never fails the job: executor code propagates, live
        # output is intact, nothing persisted.
        self.assertEqual(completed.returncode, 7, completed.stderr)
        self.assertEqual(completed.stdout.decode("utf-8").count("tick-"), 3)
        self.assertFalse(Path(log_path).exists())
        self.assertEqual(CAPTURE_CHILD_EXIT_RECIPE_FAILURE, 2)

    def test_relay_parser_requires_executor_argv_after_separator(self):
        # Relay mode without an executor argv persists nothing (malformed).
        self.assertEqual(run_capture_child(["--run-relay", "--capture", "log", "--recipe", "r"]), CAPTURE_CHILD_EXIT_RECIPE_FAILURE)
        # The parser exposes the executor argv after the "--" separator.
        options = capture_module._parse_child_options(["--capture", "log", "--recipe", "r", "--"])
        self.assertEqual(options.get("--"), [])


    def test_relay_sink_drops_backlog_instead_of_blocking(self):
        """A stalled capture child can never stall the relay (and through it
        the executor or the live stream): the bounded sink drops the backlog
        and finalizes in bounded time."""
        import time as _time
        release = threading.Event()

        class StalledStream:
            def __init__(self):
                self.closed = False

            def write(self, chunk):
                if self.closed:
                    raise OSError("stream closed")
                release.wait(5)
                raise OSError("stalled capture child")

            def flush(self):
                pass

            def close(self):
                self.closed = True

        stalled = StalledStream()
        sink = capture_module._RelayCaptureSink(stalled)
        with mock.patch.object(capture_module._RelayCaptureSink, "_DRAIN_SECONDS", 0.2):
            started = _time.monotonic()
            for _ in range(200):
                sink.write(b"x" * 4096)
            elapsed = _time.monotonic() - started
            # 200 x 4KB cannot fit the bounded queue: enqueue never blocks
            # (bounded by queue depth + drops) and the backlog is counted.
            self.assertLess(elapsed, 5.0)
            self.assertGreater(sink.dropped, 0)
            self.assertFalse(sink.broken)  # the stalled write is the child's side
            sink.finalize()
        self.assertTrue(sink.closed)
        release.set()


if __name__ == "__main__":
    unittest.main()