import contextlib
import json
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import Mock, patch

from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter
from src.worker_agent.infrastructure.security.execution_log_capture import (
    ExecutionLogCapture,
    ExecutionLogCaptureUnavailable,
)

_MODULE_ENV_NAMES = (
    "WORKER_ID",
    "WORKER_EXECUTION_LOG_ROOT",
    "WORKER_EXECUTION_LOG_RETENTION_DAYS",
    "DOCKER_HOST",
)
_saved_module_env: dict = {}
_module_temp = None


def setUpModule():
    """Isolate the execution-log capture configuration for this module: no
    ambient worker identity, retention setting or daemon location may drive
    the capture integration while the existing native tests run."""
    global _saved_module_env, _module_temp
    _saved_module_env = {name: os.environ.get(name) for name in _MODULE_ENV_NAMES}
    for name in _MODULE_ENV_NAMES:
        os.environ.pop(name, None)
    _module_temp = tempfile.TemporaryDirectory(prefix="native-runtime-tests-")
    os.environ["WORKER_EXECUTION_LOG_ROOT"] = _module_temp.name


def tearDownModule():
    global _module_temp
    for name, value in _saved_module_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    if _module_temp is not None:
        _module_temp.cleanup()
        _module_temp = None


class NativeRuntimeAdapterTests(unittest.TestCase):
    def test_native_runtime_overlays_explicit_unset_retention_fields(self):
        adapter = NativeRuntimeAdapter()
        payload = {
            "runtime_type": "native",
            "filesystem_paths": ["/srv/app"],
            "command": ["restic", "snapshots", "--json"],
            "environment": {
                "RESTIC_RETENTION_POLICY_CONFIGURED": "true",
                "RESTIC_KEEP_LAST": "7",
                "RESTIC_KEEP_HOURLY": "",
                "RESTIC_KEEP_DAILY": "",
                "RESTIC_KEEP_WEEKLY": "",
                "RESTIC_KEEP_MONTHLY": "6",
                "RESTIC_KEEP_YEARLY": "",
                "RESTIC_PRUNE": "false",
            },
        }

        with patch.dict(os.environ, {"RESTIC_KEEP_HOURLY": "99", "RESTIC_KEEP_MONTHLY": "12"}), patch.object(
            adapter, "_require_executable"
        ):
            _command, environment = adapter._command_and_environment(payload)

        self.assertEqual(environment["RESTIC_KEEP_LAST"], "7")
        self.assertEqual(environment["RESTIC_KEEP_HOURLY"], "")
        self.assertEqual(environment["RESTIC_KEEP_MONTHLY"], "6")
        self.assertEqual(environment["RESTIC_KEEP_YEARLY"], "")
        self.assertEqual(environment["RESTIC_PRUNE"], "false")

    def test_rclone_file_secret_is_materialized_and_removed_after_process(self):
        adapter = NativeRuntimeAdapter()
        secret = "[remote]\ntype = s3\nsecret_access_key = hidden-value\n"
        payload = {
            "runtime_type": "native", "filesystem_paths": ["/srv/app"],
            "command": ["restic", "snapshots", "--json"],
            "environment": {"RCLONE_CONFIG": r"C:\\run\\secrets\\rclone.conf", "RCLONE_CONF_CONTENT": secret},
            "resolved_files": [{"container_path": "/run/secrets/rclone.conf", "content": secret, "secret_name": "rclone.conf"}],
        }
        observed = {}

        def fake_popen(command, **kwargs):
            path = kwargs["env"]["RCLONE_CONFIG"]
            observed["path"] = path
            observed["content"] = pathlib.Path(path).read_text(encoding="utf-8")
            observed["directory"] = str(pathlib.Path(path).parent)
            self.assertNotIn("RCLONE_CONF_CONTENT", kwargs["env"])
            process = Mock()
            process.stdout.readline.side_effect = ["ok\n", ""]
            process.stderr.readline.side_effect = [""]
            process.wait.return_value = 0
            process.returncode = 0
            return process

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="/usr/bin/restic"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime._platform_is_windows", return_value=False
        ), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=fake_popen
        ):
            result = adapter.run_runtime_job("unused", payload)
        self.assertTrue(result["success"], result)
        self.assertEqual(observed["content"], secret)
        self.assertFalse(pathlib.Path(observed["directory"]).exists())

    def test_string_file_secret_is_redacted_from_native_logs_and_callbacks(self):
        adapter = NativeRuntimeAdapter()
        secret = "[remote]\npassword = string-file-secret-value\n"
        payload = {
            "runtime_type": "native",
            "filesystem_paths": ["/srv/app"],
            "command": ["restic", "snapshots", "--json"],
            "resolved_files": [
                {"container_path": "/run/secrets/rclone.conf", "content": secret, "secret_name": "rclone.conf"}
            ],
        }
        callbacks = []

        def fake_popen(_command, **_kwargs):
            process = Mock()
            process.stdout.readline.side_effect = [f"failed to open {secret}\n", ""]
            process.stderr.readline.side_effect = [""]
            process.wait.return_value = 1
            process.returncode = 1
            return process

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="/usr/bin/restic"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime._platform_is_windows", return_value=False
        ), patch("src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=fake_popen):
            result = adapter.run_runtime_job("unused", payload, output_callback=callbacks.append)

        rendered = "\n".join([result.get("logs", ""), result.get("stderr", ""), *callbacks])
        self.assertFalse(result["success"])
        self.assertNotIn(secret, rendered)
        self.assertIn("<redacted>", rendered)

    def test_resolved_file_materialization_failure_fails_closed_without_leaking_secret(self):
        adapter = NativeRuntimeAdapter()
        secret = "password=private-value"
        payload = {"runtime_type": "native", "filesystem_paths": ["/srv/app"], "command": ["restic", "snapshots", "--json"],
                   "resolved_files": [{"container_path": "/run/secrets/rclone.conf", "content": secret, "secret_name": "rclone"}]}
        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="/usr/bin/restic"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime._platform_is_windows", return_value=False
        ), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.os.chmod", side_effect=OSError("private-value")
        ), patch("src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen") as popen:
            result = adapter.run_runtime_job("unused", payload)
        self.assertFalse(result["success"])
        self.assertNotIn(secret, result["error"])
        self.assertNotIn("private-value", result["error"])
        self.assertNotIn("rclone.conf", result["error"])
        popen.assert_not_called()

    def test_materialized_secret_directory_is_removed_after_process_failure(self):
        adapter = NativeRuntimeAdapter()
        payload = {"runtime_type": "native", "filesystem_paths": ["/srv/app"], "command": ["restic", "snapshots", "--json"],
                   "resolved_files": [{"container_path": "/run/secrets/rclone.conf", "content": "private", "secret_name": "rclone"}]}
        observed = {}
        def fake_popen(command, **kwargs):
            observed["directory"] = os.path.dirname(kwargs["env"]["RCLONE_CONFIG"])
            process = Mock()
            process.stdout.readline.side_effect = [""]
            process.stderr.readline.side_effect = ["failure\n", ""]
            process.wait.return_value = 2
            process.returncode = 2
            return process
        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="/usr/bin/restic"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime._platform_is_windows", return_value=False
        ), patch("src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=fake_popen):
            result = adapter.run_runtime_job("unused", payload)
        self.assertFalse(result["success"])
        self.assertFalse(os.path.exists(observed["directory"]))

    def test_windows_secret_acl_grants_only_invoker_system_and_administrators(self):
        adapter = NativeRuntimeAdapter()
        secret = "safe"
        payload = {"runtime_type": "native", "filesystem_paths": ["/srv/app"], "command": ["restic", "snapshots", "--json"],
                   "resolved_files": [{"container_path": "/run/secrets/x", "content": secret}]}
        recorded = []
        def fake_popen(command, **kwargs):
            recorded.append(kwargs["env"]["RCLONE_CONFIG"] if "RCLONE_CONFIG" in kwargs["env"] else "")
            process = Mock()
            process.stdout.readline.side_effect = [""]
            process.stderr.readline.side_effect = [""]
            process.wait.return_value = 0
            process.returncode = 0
            return process
        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="/usr/bin/restic"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime._platform_is_windows", return_value=True
        ), patch("src.worker_agent.infrastructure.adapters.native_runtime._current_windows_user_sid", return_value="S-1-5-21-1"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.run"
        ) as run, patch("src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=fake_popen):
            result = adapter.run_runtime_job("unused", payload)
        self.assertTrue(result["success"])
        for call in run.call_args_list:
            args = call.args[0]
            self.assertNotIn("/setowner", args)
            grants = [args[i + 1] for i, token in enumerate(args[:-1]) if token == "/grant:r"]
            self.assertEqual(grants, ["*S-1-5-21-1:F", "*S-1-5-18:F", "*S-1-5-32-544:F"])

    def test_backup_payload_uses_filesystem_paths_json_without_mount_rewrite(self):
        adapter = NativeRuntimeAdapter(python_executable="python-test")
        payload = {
            "runtime_type": "native",
            "filesystem_paths": ["/srv/data with spaces", "C:\\Users\\Alice\\My Documents"],
            "environment": {"BACKUP_STRATEGY": "tar"},
            "command": "/root/backup.sh",
            "backup_mode": "hot",
        }
        recorded = {}

        def fake_popen(command, **kwargs):
            recorded["command"] = command
            recorded["env"] = kwargs["env"]
            process = Mock()
            process.stdout.readline.side_effect = ["Backup starting\n", ""]
            process.stderr.readline.side_effect = [""]
            process.wait.return_value = 0
            process.returncode = 0
            return process

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="/bin/python"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=fake_popen
        ):
            result = adapter.run_runtime_job("unused-image", payload)

        self.assertTrue(result["success"])
        self.assertEqual(recorded["command"], ["python-test", "-m", "src.app.main"])
        self.assertEqual(recorded["env"]["BACKUP_RUNTIME_TYPE"], "native")
        self.assertEqual(recorded["env"]["BACKUP_SOURCES_JSON"], '["/srv/data with spaces","C:\\\\Users\\\\Alice\\\\My Documents"]')
        self.assertNotIn("/backup", recorded["env"].get("BACKUP_SOURCES", ""))

    def test_frozen_backup_payload_uses_current_executable_backup_engine_subcommand(self):
        adapter = NativeRuntimeAdapter(python_executable="python-test")
        payload = {
            "runtime_type": "native",
            "filesystem_paths": ["/srv/app"],
            "environment": {"BACKUP_STRATEGY": "tar"},
            "command": "/root/backup.sh",
            "backup_mode": "hot",
        }
        recorded = {}

        def fake_popen(command, **kwargs):
            recorded["command"] = command
            recorded["env"] = kwargs["env"]
            process = Mock()
            process.stdout.readline.side_effect = ["Backup starting\n", ""]
            process.stderr.readline.side_effect = [""]
            process.wait.return_value = 0
            process.returncode = 0
            return process

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.sys.executable", "vaultline-worker.exe"), patch.object(
            sys, "frozen", True, create=True
        ), patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="vaultline-worker.exe"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=fake_popen
        ):
            result = adapter.run_runtime_job("unused-image", payload)

        self.assertTrue(result["success"])
        self.assertEqual(recorded["command"], ["vaultline-worker.exe", "backup-engine"])
        self.assertEqual(recorded["env"]["BACKUP_RUNTIME_TYPE"], "native")
        self.assertEqual(recorded["env"]["BACKUP_SOURCES_JSON"], '["/srv/app"]')

    def test_native_backup_rejects_cold_mode_before_process_launch(self):
        adapter = NativeRuntimeAdapter()
        with patch("src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen") as popen:
            result = adapter.run_runtime_job(
                "unused-image",
                {"runtime_type": "native", "backup_mode": "cold", "filesystem_paths": ["/srv/app"]},
            )

        self.assertFalse(result["success"])
        self.assertIn("hot-only", result["error"])
        popen.assert_not_called()

    def test_native_runtime_reports_missing_executable_clearly(self):
        adapter = NativeRuntimeAdapter(python_executable="missing-python")
        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value=None), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen"
        ) as popen:
            result = adapter.run_runtime_job(
                "unused-image",
                {"runtime_type": "native", "filesystem_paths": ["/srv/app"], "command": "/root/backup.sh"},
            )

        self.assertFalse(result["success"])
        self.assertIn("required executable 'missing-python' is not available", result["error"])
        popen.assert_not_called()

    def test_native_runtime_supports_restic_metadata_commands(self):
        adapter = NativeRuntimeAdapter()
        payload = {
            "runtime_type": "native",
            "filesystem_paths": ["/srv/app"],
            "environment": {"RESTIC_REPOSITORY": "local:/repo", "RESTIC_PASSWORD": "secret"},
            "command": ["restic", "snapshots", "--json"],
        }
        recorded = {}

        def fake_popen(command, **kwargs):
            recorded["command"] = command
            process = Mock()
            process.stdout.readline.side_effect = ['[{"id":"abc"}]\n', ""]
            process.stderr.readline.side_effect = [""]
            process.wait.return_value = 0
            process.returncode = 0
            return process

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="/usr/bin/restic"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=fake_popen
        ):
            result = adapter.list_restic_snapshots("unused-image", payload)

        self.assertTrue(result["success"])
        self.assertEqual(recorded["command"], ["restic", "snapshots", "--json"])
        self.assertEqual(result["snapshots"], [{"id": "abc"}])

    BACKUP_PAYLOAD = {"runtime_type": "native", "filesystem_paths": ["/srv/app"], "command": "/root/backup.sh"}

    @staticmethod
    def clock(start: float, step: float):
        """Deterministic monotonic clock: the watchdog is the only caller, one tick per read."""
        value = start - step

        def read() -> float:
            nonlocal value
            value += step
            return value

        return read

    @staticmethod
    def long_running_process(polls: int, emit_output: bool):
        """A child that stays alive for ``polls`` wait cycles, optionally printing one line per cycle.

        Output is pushed from the wait side effect and acknowledged through the
        output callback, so activity is recorded before the next verdict.
        """
        lines: queue.Queue = queue.Queue()
        delivered = threading.Event()
        remaining = {"polls": polls}
        process = Mock()
        process.stdout.readline.side_effect = lines.get
        process.stderr.readline.side_effect = [""]
        process.returncode = None

        def wait(timeout=None):
            if remaining["polls"] <= 0:
                lines.put("")
                return 0
            remaining["polls"] -= 1
            if emit_output:
                lines.put("working\n")
                delivered.wait(5)
                delivered.clear()
            raise subprocess.TimeoutExpired(["python"], timeout or 0)

        def terminate():
            remaining["polls"] = 0
            lines.put("")

        process.wait.side_effect = wait
        process.terminate.side_effect = terminate
        # Persistent-execution-log capture alerts are delivered through the
        # same callback before the executor starts; they are not executor
        # output and must not masquerade as a delivered line (otherwise an
        # acknowledge-based fake process would race its own delivery model).
        return process, (lambda chunk: None if chunk.lstrip().startswith(NativeRuntimeAdapter.EXECUTION_LOG_WARNING_PREFIX) else delivered.set())

    def run_backup(self, adapter, process, output_callback=None, payload=None):
        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value=sys.executable), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", return_value=process
        ):
            return adapter.run_runtime_job("unused-image", payload or dict(self.BACKUP_PAYLOAD), output_callback=output_callback)

    def test_native_runtime_has_no_wall_clock_unless_configured(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WORKER_RUNTIME_TIMEOUT_SECONDS", None)
            self.assertIsNone(NativeRuntimeAdapter().timeout_seconds)
        for raw, expected in (("", None), ("   ", None), ("abc", None), ("0", None), ("-5", None), ("1200", 1200.0)):
            with self.subTest(env=raw), patch.dict(os.environ, {"WORKER_RUNTIME_TIMEOUT_SECONDS": raw}):
                self.assertEqual(NativeRuntimeAdapter().timeout_seconds, expected)
        self.assertEqual(NativeRuntimeAdapter(timeout_seconds=0.2).timeout_seconds, 0.2)
        adapter = NativeRuntimeAdapter(timeout_seconds=600)
        self.assertEqual(adapter._runtime_timeout({"timeout_seconds": 7200}), 7200.0)
        self.assertEqual(adapter._runtime_timeout({"timeout_seconds": "bogus"}), 600.0)
        self.assertIsNone(NativeRuntimeAdapter()._runtime_timeout({"timeout_seconds": "bogus"}))

    def test_native_runtime_keeps_a_slow_but_talking_job_alive_without_a_wall_clock(self):
        adapter = NativeRuntimeAdapter(python_executable=sys.executable)
        self.assertIsNone(adapter.timeout_seconds)
        self.assertEqual(adapter.idle_timeout_seconds, 900.0)
        # Ten polls, ten minutes apart: 100 minutes of (patched) wall clock, well past the old 30-minute cap.
        adapter._monotonic = self.clock(1000.0, 600.0)
        process, acknowledge = self.long_running_process(polls=10, emit_output=True)

        result = self.run_backup(adapter, process, output_callback=acknowledge)

        self.assertTrue(result["success"], result)
        self.assertNotIn("error", result)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    def test_native_runtime_idle_watchdog_terminates_a_silent_job_with_an_honest_message(self):
        adapter = NativeRuntimeAdapter(python_executable=sys.executable)
        adapter._monotonic = self.clock(1000.0, 600.0)
        process, _acknowledge = self.long_running_process(polls=10, emit_output=False)

        result = self.run_backup(adapter, process)

        self.assertFalse(result["success"])
        self.assertFalse(result["canceled"])
        self.assertEqual(result["status_code"], 124)
        self.assertEqual(result["error"], "native runtime produced no output for 900 seconds")
        self.assertTrue(result["process_terminated"])
        process.terminate.assert_called_once()
        process.kill.assert_not_called()

    def test_native_runtime_idle_watchdog_can_be_disabled_and_is_clamped(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WORKER_RUNTIME_IDLE_TIMEOUT_SECONDS", None)
            self.assertEqual(NativeRuntimeAdapter().idle_timeout_seconds, 900.0)
        for raw, expected in (
            ("0", None),
            ("", None),
            ("abc", 900.0),
            ("-1", 900.0),
            ("5", 30.0),
            ("1200", 1200.0),
            ("999999999", 86400.0),
        ):
            with self.subTest(env=raw), patch.dict(os.environ, {"WORKER_RUNTIME_IDLE_TIMEOUT_SECONDS": raw}):
                self.assertEqual(NativeRuntimeAdapter().idle_timeout_seconds, expected)

        adapter = NativeRuntimeAdapter(python_executable=sys.executable, idle_timeout_seconds=0)
        self.assertIsNone(adapter.idle_timeout_seconds)
        adapter._monotonic = self.clock(1000.0, 3600.0)
        process, _acknowledge = self.long_running_process(polls=5, emit_output=False)

        result = self.run_backup(adapter, process)

        self.assertTrue(result["success"], result)
        process.terminate.assert_not_called()

    def test_native_runtime_explicit_timeout_terminates_process_without_waiting_long(self):
        cases = (
            ("constructor", {}, dict(timeout_seconds=0.2)),
            ("environment", {"WORKER_RUNTIME_TIMEOUT_SECONDS": "0.2"}, {}),
            ("payload", {}, {}),
        )
        for name, environ, kwargs in cases:
            with self.subTest(source=name), patch.dict(os.environ, environ):
                adapter = NativeRuntimeAdapter(python_executable=sys.executable, **kwargs)
                adapter._monotonic = self.clock(100.0, 0.3)
                process = Mock()
                process.stdout.readline.side_effect = ["working\n", ""]
                process.stderr.readline.side_effect = [""]
                process.wait.return_value = 0
                process.returncode = None
                payload = dict(self.BACKUP_PAYLOAD)
                if name == "payload":
                    payload["timeout_seconds"] = 0.2

                result = self.run_backup(adapter, process, payload=payload)

                self.assertFalse(result["success"])
                self.assertFalse(result["canceled"])
                self.assertEqual(result["status_code"], 124)
                self.assertEqual(result["error"], "native runtime timed out after 0.2 seconds")
                process.terminate.assert_called()
                process.kill.assert_not_called()

    def test_native_runtime_timeout_reports_cleanup_failure_when_kill_wait_does_not_finish(self):
        adapter = NativeRuntimeAdapter(timeout_seconds=0.2, python_executable=sys.executable)
        adapter._monotonic = self.clock(100.0, 0.3)
        process = Mock()
        process.stdout.readline.side_effect = ["working\n", ""]
        process.stderr.readline.side_effect = [""]
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["python"], 2),
            subprocess.TimeoutExpired(["python"], 2),
        ]
        process.returncode = None

        result = self.run_backup(adapter, process)

        self.assertFalse(result["success"])
        self.assertFalse(result["canceled"])
        self.assertEqual(result["status_code"], 124)
        self.assertIn("timed out after 0.2 seconds", result["error"])
        self.assertIn("could not confirm process termination", result["error"])
        self.assertFalse(result["process_terminated"])
        process.terminate.assert_called()
        process.kill.assert_called()

    def test_native_runtime_cancellation_stops_process_and_reports_canceled(self):
        adapter = NativeRuntimeAdapter(python_executable=sys.executable)
        process = Mock()
        process.stdout.readline.side_effect = ["working\n", ""]
        process.stderr.readline.side_effect = [""]
        process.wait.return_value = 0
        process.returncode = 0

        cancel_calls = {"count": 0}

        def cancel_after_launch():
            cancel_calls["count"] += 1
            return cancel_calls["count"] > 1

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value=sys.executable), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", return_value=process
        ):
            result = adapter.run_runtime_job(
                "unused-image",
                {"runtime_type": "native", "filesystem_paths": ["/srv/app"], "command": "/root/backup.sh"},
                cancel_check=cancel_after_launch,
            )

        self.assertFalse(result["success"])
        self.assertTrue(result["canceled"])
        self.assertEqual(result["status_code"], 130)
        process.terminate.assert_called()


class NativeExecutionLogCaptureTests(unittest.TestCase):
    """T2 (S1-S6, persistent-worker-execution-logs.md): the native runtime
    wraps its validated executor command into an executor-owning capture
    relay whose detached capture child keeps persisting redacted evidence
    after the worker dies; any capture failure continues, alerts locally and
    through the job progress callback, and never blocks, kills or falsely
    fails the backup."""

    SECRET = "hunter2"
    WORKER_ID = "worker-a"
    JOB_ID = "job-42"
    WARNING_PREFIX = "[persistent-execution-log]"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="native-exec-log-capture-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.recorded_commands = []
        # Environment isolation for paths that bypass the module setup.
        self.saved_root = os.environ.get("WORKER_EXECUTION_LOG_ROOT")
        os.environ["WORKER_EXECUTION_LOG_ROOT"] = self.root

        def restore_root():
            if self.saved_root is None:
                os.environ.pop("WORKER_EXECUTION_LOG_ROOT", None)
            else:
                os.environ["WORKER_EXECUTION_LOG_ROOT"] = self.saved_root

        self.addCleanup(restore_root)

    def payload(self, **extra):
        payload = {
            "runtime_type": "native",
            "filesystem_paths": ["/srv/app"],
            "command": "/root/backup.sh",
            "environment": {"RESTIC_PASSWORD": self.SECRET},
            "_job_id": self.JOB_ID,
        }
        payload.update(extra)
        return payload

    def runtime(self, worker_id=WORKER_ID, **kwargs):
        adapter = NativeRuntimeAdapter(python_executable="python-test", **kwargs)
        adapter.worker_id = worker_id
        return adapter

    def patch_capture(self, *, frame_raises=None, real_prune=False):
        """Isolate the capture seams: the retention sweep never touches the
        real host log root. The ephemeral recipe frame encoding stays real
        (it never writes to disk); ``frame_raises`` simulates an encoding
        failure. The relay wrapping and the evidence bookkeeping stay real
        throughout."""
        seams = {"prunes": []}

        def record_prune(*args, **kwargs):
            seams["prunes"].append((args, kwargs))
            return {"deleted": 0, "failed": 0, "errors": []}

        patches = []
        if frame_raises is not None:
            from src.worker_agent.infrastructure.security.execution_log_recipe_stream import encode_recipe_frame as _real_encode
            def failing_encode(values):
                raise frame_raises
            patches.append(
                patch(
                    "src.worker_agent.infrastructure.adapters.native_runtime.encode_recipe_frame",
                    side_effect=failing_encode,
                )
            )
        if not real_prune:
            patches.append(patch.object(ExecutionLogCapture, "prune", side_effect=record_prune))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return contextlib.nullcontext(seams)

    def fake_executor(self, stdout_lines=("ok\n",), exit_code=0, stderr_lines=("",), create_evidence=True):
        """Synthetic executor/relay stand-in: records the Popen command and,
        when the command is the capture-relay argv, models the relay's real
        executor-owning behavior of having the detached capture child create
        the evidence file (finalized asynchronously with [completed])."""
        recorded = self.recorded_commands

        def fake_popen(command, **kwargs):
            recorded.append(list(command) if isinstance(command, list) else command)
            if create_evidence and isinstance(command, list) and "--capture" in command:
                log_path = pathlib.Path(command[command.index("--capture") + 1])
                log_path.parent.mkdir(parents=True, exist_ok=True)
                if create_evidence is True:
                    evidence = "[started]\n[attached]\nok evidence\n[completed]\n"
                else:
                    evidence = "[started]\n[attached]\npartial\n"
                log_path.write_text(evidence, encoding="utf-8")
            process = Mock()
            process.stdout.readline.side_effect = list(stdout_lines) + [""]
            process.stderr.readline.side_effect = list(stderr_lines) + [""]
            process.wait.return_value = exit_code
            process.returncode = exit_code
            return process

        return fake_popen

    def run_job(self, adapter, *, fake_popen, callbacks=None, cancel_check=None, payload=None):
        real_popen = subprocess.Popen

        def popen_router(command, *args, **kwargs):
            # The real one-shot recipe hardens itself with ``whoami`` and
            # ``icacls`` on Windows; those support subprocesses stay real
            # while the executor process is synthetic.
            if command and command[0] in ("whoami", "icacls"):
                return real_popen(command, *args, **kwargs)
            return fake_popen(command, *args, **kwargs)

        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value=sys.executable), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen", side_effect=popen_router
        ):
            return adapter.run_runtime_job(
                "unused-image",
                payload or self.payload(),
                cancel_check=cancel_check,
                output_callback=(callbacks.append if callbacks is not None else None),
            )

    def warnings_in(self, lines):
        return [line for line in lines if self.WARNING_PREFIX in line]

    def managed_log_parts(self, log_path):
        return pathlib.Path(log_path).relative_to(self.root).parts

    def test_worker_identity_comes_from_the_registered_worker_environment(self):
        with patch.dict(os.environ, {"WORKER_ID": "registered-worker"}):
            self.assertEqual(NativeRuntimeAdapter().worker_id, "registered-worker")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WORKER_ID", None)
            self.assertIsNone(NativeRuntimeAdapter().worker_id)

    def test_execution_log_root_prefers_the_configured_persistent_state_directory(self):
        """S1: the durable state/recovery configuration decides where the
        fixed execution-log structure lives, mirroring the Docker draft:
        explicit override, then WORKER_STATE_DIR, then the recovered job
        journal directory, then the credential-file directory."""
        custom = pathlib.Path(self.root) / "custom-root"
        state = pathlib.Path(self.root) / "state"
        recovery = pathlib.Path(self.root) / "recovery" / "worker_state.json"
        credential = pathlib.Path(self.root) / "config" / "worker_credentials.json"
        adapter = self.runtime()
        # The setUp override must not mask the state-directory precedence.
        os.environ.pop("WORKER_EXECUTION_LOG_ROOT", None)
        with patch.dict(os.environ, {"WORKER_EXECUTION_LOG_ROOT": str(custom),
                                     "WORKER_STATE_DIR": str(state),
                                     "WORKER_JOB_RECOVERY_FILE": str(recovery),
                                     "WORKER_CREDENTIAL_FILE": str(credential)}):
            self.assertEqual(pathlib.Path(adapter._execution_log_root()), custom)
        with patch.dict(os.environ, {"WORKER_STATE_DIR": str(state),
                                     "WORKER_JOB_RECOVERY_FILE": str(recovery),
                                     "WORKER_CREDENTIAL_FILE": str(credential)}):
            self.assertEqual(pathlib.Path(adapter._execution_log_root()), state / "execution-logs")
        with patch.dict(os.environ, {"WORKER_JOB_RECOVERY_FILE": str(recovery),
                                     "WORKER_CREDENTIAL_FILE": str(credential)}):
            self.assertEqual(pathlib.Path(adapter._execution_log_root()), recovery.parent / "execution-logs")
        with patch.dict(os.environ, {"WORKER_CREDENTIAL_FILE": str(credential)}):
            self.assertEqual(pathlib.Path(adapter._execution_log_root()), credential.parent / "execution-logs")
        for name in ("WORKER_EXECUTION_LOG_ROOT", "WORKER_STATE_DIR", "WORKER_JOB_RECOVERY_FILE", "WORKER_CREDENTIAL_FILE"):
            os.environ.pop(name, None)
        # The committed path-policy default stays the native fallback.
        self.assertEqual(pathlib.Path(adapter._execution_log_root()), pathlib.Path(ExecutionLogCapture.default_root()))

    def test_capture_path_reserves_native_names_inside_the_state_root(self):
        """Same-second captures shift to a deterministic sibling name and
        stay inside the worker/native managed structure."""
        adapter = self.runtime()
        root = str(pathlib.Path(self.root) / "state" / "execution-logs")
        first_log, first_recipe = adapter._execution_log_capture_path(self.WORKER_ID, self.JOB_ID, root)
        # The recipe is written by the setup and the log appears once the
        # capture child starts writing evidence; occupy both names.
        first_log.parent.mkdir(parents=True, exist_ok=True)
        first_log.touch()
        first_recipe.touch()
        second_log, second_recipe = adapter._execution_log_capture_path(self.WORKER_ID, self.JOB_ID, root)
        for log_path, recipe_path in ((first_log, first_recipe), (second_log, second_recipe)):
            parts = log_path.relative_to(root).parts
            self.assertEqual(parts[0], self.WORKER_ID)
            self.assertEqual(parts[1], "native")
            self.assertEqual(str(recipe_path), str(log_path) + ".recipe.json")
        self.assertNotEqual(first_log.name, second_log.name)
        self.assertRegex(second_log.name, r"-1\.log$")

    def test_successful_job_wraps_a_persistent_executor_owning_capture_relay(self):
        with self.patch_capture() as seams:
            callbacks = []
            result = self.run_job(self.runtime(), fake_popen=self.fake_executor(), callbacks=callbacks)

        self.assertTrue(result["success"], result)
        self.assertIn("ok", result["logs"])
        # The executor command runs exactly once, wrapped in the executor-
        # owning relay: secrets-free argv, durable fixed paths, native kind.
        command = self.recorded_commands[0]
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[2], "--run-relay")
        log_path = pathlib.Path(command[command.index("--capture") + 1])
        # Ephemeral mode: --recipe-stream 1 instead of --recipe <path>.
        self.assertIn("--recipe-stream", command)
        self.assertEqual(command[command.index("--recipe-stream") + 1], "1")
        self.assertNotIn("--recipe", command)
        self.assertEqual(command[command.index("--") + 1:], ["python-test", "-m", "src.app.main"])
        parts = self.managed_log_parts(log_path)
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], self.WORKER_ID)
        self.assertEqual(parts[1], "native")
        self.assertRegex(parts[2], r"^\d{8}$")
        self.assertTrue(pathlib.Path(parts[3]).name.startswith(f"{self.JOB_ID}-"))
        self.assertEqual(pathlib.Path(parts[3]).suffix, ".log")
        # No recipe file is ever written to disk (ephemeral transfer).
        self.assertEqual([], list(pathlib.Path(self.root).rglob("*.recipe.json")))
        self.assertNotIn(self.SECRET, str(command))
        # S3: the configured retention sweep runs on the same root.
        self.assertEqual(seams["prunes"][0][0], (self.root,))
        # Existing live progress semantics are untouched and no alert fired.
        self.assertIn("ok\n", callbacks)
        self.assertEqual([], self.warnings_in(callbacks))

    def test_capture_setup_failure_keeps_the_backup_running_and_alerts_safely(self):
        with self.patch_capture(
            frame_raises=ExecutionLogCaptureUnavailable("redaction recipe frame could not be encoded")
        ):
            callbacks = []
            adapter = self.runtime()
            with self.assertLogs("src.worker_agent.infrastructure.adapters.native_runtime", level="WARNING") as logged:
                result = self.run_job(adapter, fake_popen=self.fake_executor(), callbacks=callbacks)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["logs"], "ok\n")
        self.assertEqual(ExecutionLogCapture.prune.call_count, 0)
        # Exactly one actionable, safe warning: worker log and CP callback.
        warnings = self.warnings_in(callbacks)
        self.assertEqual(len(warnings), 1)
        self.assertIn(self.JOB_ID, warnings[0])
        self.assertIn("redaction recipe frame could not be encoded", warnings[0])
        self.assertNotIn(self.SECRET, warnings[0])
        self.assertNotIn(self.SECRET, "\n".join(logged.output))
        # No capture child was ever spawned by the adapter and the executor
        # ran directly (previous behavior), not wrapped.
        self.assertEqual(self.recorded_commands[0], ["python-test", "-m", "src.app.main"])
        self.assertNotIn("--run-relay", str(self.recorded_commands))

    def test_recipe_failure_skips_capture_and_alerts_without_false_failure(self):
        with self.patch_capture(
            frame_raises=ExecutionLogCaptureUnavailable("redaction recipe frame could not be encoded")
        ):
            callbacks = []
            result = self.run_job(self.runtime(), fake_popen=self.fake_executor(), callbacks=callbacks)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["logs"], "ok\n")
        self.assertNotIn("--run-relay", str(self.recorded_commands[0]))
        warnings = self.warnings_in(callbacks)
        self.assertEqual(len(warnings), 1)
        self.assertIn("redaction recipe frame", warnings[0])

    def test_no_recipe_file_is_ever_written_to_disk(self):
        """Ephemeral secret transfer: the capture setup encodes secrets as a
        bounded in-memory frame and pipes it to the relay child's stdin; no
        recipe file is ever created on the host, so a crash between
        preparation and consumption can never leave plaintext on disk."""
        with self.patch_capture():
            capture = self.runtime()._start_execution_log_capture(self.payload(), {self.SECRET}, None)
        self.assertIsNotNone(capture)
        self.assertIn("recipe_frame", capture)
        self.assertNotIn("recipe_path", capture)
        # No recipe file exists anywhere under the persistent root.
        self.assertEqual([], list(pathlib.Path(self.root).rglob("*.recipe.json")))
        # The frame is binary bytes, not a file path.
        self.assertIsInstance(capture["recipe_frame"], bytes)

    def test_capture_relays_through_ephemeral_recipe_stream(self):
        """S4: the relay wraps the executor with --recipe-stream, proving the
        ephemeral handoff path is selected for every captured job."""
        with self.patch_capture():
            callbacks = []
            result = self.run_job(self.runtime(), fake_popen=self.fake_executor(), callbacks=callbacks)

        self.assertTrue(result["success"], result)
        relay_commands = [cmd for cmd in self.recorded_commands if "--run-relay" in str(cmd)]
        self.assertEqual(len(relay_commands), 1, self.recorded_commands)
        self.assertIn("--recipe-stream", relay_commands[0])

    def test_unknown_worker_identity_skips_capture_and_alerts(self):
        with self.patch_capture():
            callbacks = []
            result = self.run_job(
                self.runtime(worker_id=None),
                fake_popen=self.fake_executor(),
                callbacks=callbacks,
            )

        self.assertTrue(result["success"], result)
        warnings = self.warnings_in(callbacks)
        self.assertEqual(len(warnings), 1)
        self.assertIn("worker identity", warnings[0])

    def test_invalid_worker_identity_skips_capture_and_alerts(self):
        with self.patch_capture():
            callbacks = []
            result = self.run_job(
                self.runtime(worker_id="bad worker id!"),
                fake_popen=self.fake_executor(),
                callbacks=callbacks,
            )

        self.assertTrue(result["success"], result)
        warnings = self.warnings_in(callbacks)
        self.assertEqual(len(warnings), 1)
        self.assertIn("identity", warnings[0])

    def test_missing_evidence_keeps_the_result_and_alerts_the_control_plane(self):
        """The relay ran but no evidence file appeared: the bounded drain
        detects the capture-child failure and alerts once, safely; the job
        result is untouched."""
        with self.patch_capture(), patch.object(NativeRuntimeAdapter, "EXECUTION_LOG_DRAIN_SECONDS", 0.2):
            callbacks = []
            result = self.run_job(
                self.runtime(),
                fake_popen=self.fake_executor(create_evidence=False),
                callbacks=callbacks,
            )

        self.assertTrue(result["success"], result)
        warnings = self.warnings_in(callbacks)
        self.assertEqual(len(warnings), 1)
        self.assertIn("execution log file could not be created", warnings[0])

    def test_unfinalized_evidence_keeps_partial_evidence_without_false_failure(self):
        """Evidence without its completion marker still counts as persisted
        partial evidence: no false failure, no alert."""
        with self.patch_capture(), patch.object(NativeRuntimeAdapter, "EXECUTION_LOG_DRAIN_SECONDS", 0.2):
            callbacks = []
            result = self.run_job(self.runtime(), fake_popen=self.fake_executor(create_evidence="partial"), callbacks=callbacks)

        self.assertTrue(result["success"], result)
        self.assertEqual([], self.warnings_in(callbacks))
        # Exactly one evidence file was created by the modeled child.
        logs = [path for path in pathlib.Path(self.root).rglob("*.log")]
        self.assertEqual(len(logs), 1)
        self.assertIn("partial", logs[0].read_text(encoding="utf-8"))
        self.assertNotIn("[completed]", logs[0].read_text(encoding="utf-8"))

    def test_cancellation_keeps_prior_result_and_does_not_wait(self):
        cancel_calls = {"count": 0}

        def cancel_after_launch():
            cancel_calls["count"] += 1
            return cancel_calls["count"] > 1

        with self.patch_capture(), patch.object(NativeRuntimeAdapter, "EXECUTION_LOG_DRAIN_SECONDS", 0.2):
            callbacks = []
            result = self.run_job(
                self.runtime(),
                fake_popen=self.fake_executor(create_evidence=False),
                cancel_check=cancel_after_launch,
                payload=self.payload(timeout_seconds=2),
            )

        self.assertFalse(result["success"])
        self.assertTrue(result["canceled"])
        self.assertEqual(result["status_code"], 130)

    def test_job_without_capture_conditions_keeps_the_previous_result_shape(self):
        """PRESERVE: previous run_runtime_job behavior holds when capture
        cannot run (no registered identity): same success shape, same logs,
        and the executor command is run directly."""
        with self.patch_capture():
            callbacks = []
            result = self.run_job(
                self.runtime(worker_id=None),
                fake_popen=self.fake_executor(),
                callbacks=callbacks,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["status_code"], 0)
        self.assertEqual(result["logs"], "ok\n")
        self.assertEqual(result["stderr"], "")
        self.assertIn("ok\n", callbacks)
        # Only the documented identity alert may appear (its exact reason and
        # count are asserted by test_unknown_worker_identity_skips_capture_and_alerts).
        self.assertEqual(len(self.warnings_in(callbacks)), 1)
        # The executor command stayed direct (no relay wrapping).
        self.assertEqual(self.recorded_commands[0], ["python-test", "-m", "src.app.main"])


class NativeExecutionLogCaptureProcessTests(unittest.TestCase):
    """Real-process evidence for the native capture integration (S1-S6):
    actual executor output through a real pipe to a real detached capture
    child; real capture and spawner failures. No Docker, no production."""

    SECRET = "p3pp3r-s3cret-value"
    WORKER_ID = "worker-proc"
    COMPLETION_TIMEOUT = 30.0

    EXECUTOR_FAST = """
    import sys
    print("executor stdout p3pp3r-s3cret-value", flush=True)
    sys.stderr.write("executor stderr p3pp3r-s3cret-value\\n")
    sys.stderr.flush()
    sys.exit(3)
    """

    EXECUTOR_TICKING = """
    import sys, time
    for index in range(40):
        print(f"tick-{index} p3pp3r-s3cret-value", flush=True)
        time.sleep(0.05)
    print("done", flush=True)
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="native-exec-log-proc-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.saved = {}
        for name in ("WORKER_EXECUTION_LOG_ROOT", "WORKER_ID"):
            self.saved[name] = os.environ.get(name)

        def restore_env():
            for name, value in self.saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        os.environ["WORKER_EXECUTION_LOG_ROOT"] = self.root
        os.environ.pop("WORKER_ID", None)
        self.addCleanup(restore_env)

    def adapter(self, executor_script):
        adapter = NativeRuntimeAdapter(python_executable=sys.executable)
        adapter.worker_id = self.WORKER_ID
        script = textwrap.dedent(executor_script)
        adapter._backup_engine_command = lambda: [sys.executable, "-c", script]
        return adapter

    def payload(self, **extra):
        payload = {
            "runtime_type": "native",
            "filesystem_paths": ["/srv/app"],
            "command": "/root/backup.sh",
            "environment": {"RESTIC_PASSWORD": self.SECRET},
            "_job_id": "job-proc-1",
        }
        payload.update(extra)
        return payload

    def worker_log_directory(self, worker_id=WORKER_ID):
        base = pathlib.Path(self.root) / worker_id / "native"
        dates = list(base.iterdir()) if base.exists() else []
        for date in dates:
            for path in date.iterdir():
                if path.name.endswith(".log"):
                    yield path

    def wait_for(self, predicate, timeout=COMPLETION_TIMEOUT, interval=0.1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return predicate()

    def test_job_result_and_exit_codes_are_unchanged_and_evidence_persists_redacted(self):
        callbacks = []
        with self.assertLogs("src.worker_agent.infrastructure.adapters.native_runtime", level="INFO") as logged:
            result = self.adapter(self.EXECUTOR_FAST).run_runtime_job(
                "unused-image", self.payload(), output_callback=callbacks.append
            )

        # Existing execution semantics are untouched (nonzero exit preserved).
        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 3)
        self.assertIn("executor stdout", "".join(callbacks), callbacks)
        self.assertNotIn(self.SECRET, "".join(callbacks))
        self.assertIn("<redacted>", "".join(callbacks))
        # A dedicated persistent log for this job exists under the fixed root.
        logs = list(self.worker_log_directory())
        self.assertEqual(len(logs), 1, logs)
        persisted = self.wait_for(lambda: any("[completed]" in path.read_text(encoding="utf-8", errors="replace") for path in logs))
        self.assertTrue(persisted, "capture child did not finalize the log in time")
        content = logs[0].read_text(encoding="utf-8", errors="replace")
        # Redaction precedes disk: the secret value never reached the file.
        self.assertNotIn(self.SECRET, content)
        self.assertIn("<redacted>", content)
        self.assertIn("executor stdout", content)
        self.assertIn("executor stderr", content)
        self.assertIn("[completed]", content)
        # The one-shot recipe was consumed by the capture child.
        self.assertEqual([], [path.name for path in pathlib.Path(self.root).rglob("*.recipe.json")])
        self.assertIn("Persistent execution log capture started", "\n".join(logged.output))

    def test_corrupt_recipe_makes_the_real_child_fail_and_the_job_continue(self):
        # Ephemeral mode: inject an invalid frame so the relay child cannot
        # parse the recipe. The relay fails-open (skips capture) and the
        # executor still runs with its exit code preserved.
        callbacks = []
        with patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.encode_recipe_frame",
            return_value=b"not-a-valid-frame",
        ):
            result = self.adapter(self.EXECUTOR_FAST).run_runtime_job(
                "unused-image", self.payload(), output_callback=callbacks.append
            )

        # The executor result is untouched by the failed capture child.
        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 3)
        self.assertIn("executor stdout", "".join(callbacks), callbacks)
        self.assertNotIn(self.SECRET, "".join(callbacks))
        self.assertIn("<redacted>", "".join(callbacks))
        warnings = [line for line in callbacks if "[persistent-execution-log]" in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("execution log file could not be created", warnings[0])
        self.assertNotIn(self.SECRET, warnings[0])
        # Fail-closed: the rejected recipe persisted nothing.
        self.assertEqual([], [path.name for path in pathlib.Path(self.root).rglob("*.log")])

    def test_unusable_capture_root_continues_the_job_and_alerts(self):
        blocker = pathlib.Path(self.root) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        os.environ["WORKER_EXECUTION_LOG_ROOT"] = str(blocker)
        callbacks = []
        result = self.adapter(self.EXECUTOR_FAST).run_runtime_job(
            "unused-image", self.payload(), output_callback=callbacks.append
        )

        # The executor result is untouched and real output still streamed.
        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 3)
        self.assertIn("executor stdout", "".join(callbacks), callbacks)
        self.assertNotIn(self.SECRET, "".join(callbacks))
        self.assertIn("<redacted>", "".join(callbacks))
        warnings = [line for line in callbacks if "[persistent-execution-log]" in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("execution log file could not be created", warnings[0])

    def _pid_alive(self, pid):
        if os.name == "posix":
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                return False
            return True
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    def test_capture_child_survives_worker_death_and_captures_post_death_output(self):
        """Real spawner death (S5): the driver worker is killed mid-job while
        the independently running executor keeps producing output owned by
        the capture relay; every post-death line must still be persisted in
        redacted form by the surviving detached capture child."""
        driver_code = textwrap.dedent(
            f"""
            import json, os, sys, time, threading
            sys.path.insert(0, {os.getcwd()!r})
            os.environ["WORKER_EXECUTION_LOG_ROOT"] = {self.root!r}
            os.environ.pop("WORKER_ID", None)
            from unittest.mock import patch
            from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter
            from src.worker_agent.infrastructure.security.execution_log_capture import ExecutionLogCapture

            script = {textwrap.dedent(self.EXECUTOR_TICKING)!r}
            adapter = NativeRuntimeAdapter(python_executable=sys.executable)
            adapter.worker_id = "worker-proc"
            adapter._backup_engine_command = lambda: [sys.executable, "-c", script]
            payload = {{
                "runtime_type": "native",
                "filesystem_paths": ["/srv/app"],
                "command": "/root/backup.sh",
                "environment": {{"RESTIC_PASSWORD": "p3pp3r-s3cret-value"}},
                "_job_id": "job-death-1",
            }}
            with patch.dict(os.environ, {{"WORKER_STATE_DIR": {self.root!r}}}):
                first_output = threading.Event()
                def callback(line):
                    if "tick-0" in line:
                        first_output.set()
                job = threading.Thread(
                    target=lambda: adapter.run_runtime_job("unused-image", payload, output_callback=callback),
                    daemon=True,
                )
                job.start()
                # Die mid-job: the executor must keep producing output that is
                # owned by an executor-owning relay, not by this worker.
                if not first_output.wait(20):
                    os._exit(42)
                os._exit(97)  # abrupt worker death: no cleanup or EOF runs
            """
        )
        driver_path = pathlib.Path(self.tmp.name) / "worker_driver.py"
        driver_path.write_text(driver_code, encoding="utf-8")
        driver = subprocess.Popen(
            [sys.executable, str(driver_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            cwd=os.getcwd(),
        )
        try:
            _out, err = driver.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            driver.kill()
            driver.communicate(timeout=10)
            self.fail("driver worker did not die on schedule")
        self.assertEqual(driver.returncode, 97, err)
        # The executor-owning relay spawns the detached capture child, whose
        # own pid is recorded in the log's [started] event.
        appeared = self.wait_for(lambda: any(log_path.name.endswith(".log") for log_path in self.worker_log_directory("worker-proc")))
        self.assertTrue(appeared, "surviving capture child never created the job log")
        logs = [log_path for log_path in self.worker_log_directory("worker-proc")]
        self.assertEqual(len(logs), 1, logs)
        log_path = logs[0]
        self.wait_for(lambda: "[started] pid=" in log_path.read_text(encoding="utf-8", errors="replace"))
        import re as _re
        match = _re.search(r"\[started\] pid=(\d+)", log_path.read_text(encoding="utf-8", errors="replace"))
        self.assertIsNotNone(match, log_path.read_text(encoding="utf-8", errors="replace")[:400])
        pid = int(match.group(1))
        # S5: the independently running executor outlives the worker; its
        # post-death output must be captured redacted as well (all 40 ticks).
        fully_captured = self.wait_for(lambda: log_path.read_text(encoding="utf-8", errors="replace").count("tick-") >= 40)
        self.assertTrue(fully_captured, "post-death executor output was not captured")
        finalized = self.wait_for(lambda: "[completed]" in log_path.read_text(encoding="utf-8", errors="replace"))
        content = log_path.read_text(encoding="utf-8", errors="replace")
        self.assertTrue(finalized, f"capture child did not finalize: {content[:400]}")
        self.assertIn("[completed]", content)
        self.assertNotIn("p3pp3r-s3cret-value", content)
        self.assertIn("<redacted>", content)
        self.assertIn("tick-0", content)
        # The detached child is not leaked: it exits after finalizing.
        exited = self.wait_for(lambda: not self._pid_alive(pid))
        self.assertTrue(exited, "detached capture child survived past finalization")
        self.assertEqual([], [path.name for path in pathlib.Path(self.root).rglob("*.recipe.json")])
if __name__ == "__main__":
    unittest.main()
