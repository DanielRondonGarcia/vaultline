import os
import pathlib
import queue
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter


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
        return process, (lambda _chunk: delivered.set())

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


if __name__ == "__main__":
    unittest.main()
