import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from src.worker_agent.windows_service import ServiceError


class WorkerNativeCLITests(unittest.TestCase):
    def test_backup_engine_dispatches_to_app_main_and_returns_zero(self):
        from src.worker_agent import cli

        with patch("src.app.main.main", return_value=None) as app_main:
            exit_code = cli.main(["backup-engine"])

        self.assertEqual(exit_code, 0)
        app_main.assert_called_once_with()

    def test_backup_engine_dispatch_preserves_system_exit_code(self):
        from src.worker_agent import cli

        with patch("src.app.main.main", side_effect=SystemExit(7)):
            exit_code = cli.main(["backup-engine"])

        self.assertEqual(exit_code, 7)

    def test_backup_engine_is_not_listed_in_public_help(self):
        from src.worker_agent import cli

        help_text = cli.build_parser().format_help()

        self.assertIn("daemon", help_text)
        self.assertIn("enroll", help_text)
        self.assertIn("self-check", help_text)
        self.assertNotIn("backup-engine", help_text)

    def test_daemon_defaults_to_native_continuous_worker_main(self):
        from src.worker_agent import cli

        calls = []

        with patch.dict(os.environ, {}, clear=True), patch(
            "src.worker_agent.main.main", side_effect=lambda: calls.append("main")
        ):
            exit_code = cli.main(["daemon"])
            self.assertEqual(os.environ["WORKER_RUNTIME"], "native")
            self.assertEqual(os.environ["WORKER_RUN_ONCE"], "false")

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, ["main"])

    def test_daemon_once_flag_is_explicit_debug_mode(self):
        from src.worker_agent import cli

        with patch.dict(os.environ, {"WORKER_RUNTIME": "docker", "WORKER_RUN_ONCE": "false"}, clear=True), patch(
            "src.worker_agent.main.main"
        ) as worker_main:
            exit_code = cli.main(["daemon", "--once"])
            self.assertEqual(os.environ["WORKER_RUNTIME"], "native")
            self.assertEqual(os.environ["WORKER_RUN_ONCE"], "true")

        self.assertEqual(exit_code, 0)
        worker_main.assert_called_once_with()

    def test_self_check_reports_presence_without_secret_values(self):
        from src.worker_agent import cli

        output = io.StringIO()
        secret_value = "super-secret-token-value"
        env = {
            "CONTROL_PLANE_URL": "https://cp.example.invalid/private-path",
            "WORKER_ENROLLMENT_TOKEN": secret_value,
            "WORKER_CREDENTIAL_FILE": "missing-worker-credentials.json",
            "WORKER_ID": "worker-private-id",
            "RESTIC_PASSWORD": "restic-secret",
        }

        with patch.dict(os.environ, env, clear=True), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.NativeRuntimeAdapter.self_check",
            return_value={
                "native_available": True,
                "runtime_type": "native",
                "executables": {"restic": False, "rclone": True},
            },
        ):
            with redirect_stdout(output):
                exit_code = cli.main(["self-check"])

        payload = json.loads(output.getvalue())
        rendered = json.dumps(payload, sort_keys=True)
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["configuration"]["control_plane_url_configured"])
        self.assertTrue(payload["configuration"]["enrollment_token_configured"])
        self.assertTrue(payload["configuration"]["worker_id_configured"])
        self.assertFalse(payload["configuration"]["credential_file_exists"])
        self.assertEqual(payload["runtime"]["executables"], {"restic": False, "rclone": True})
        self.assertNotIn(secret_value, rendered)
        self.assertNotIn("cp.example", rendered)
        self.assertNotIn("worker-private-id", rendered)
        self.assertNotIn("restic-secret", rendered)


def _run_cli(argv):
    from src.worker_agent import cli

    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli.main(argv)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class WorkerServiceCommandParserTests(unittest.TestCase):
    def test_service_subcommand_lists_all_verbs(self):
        from src.worker_agent import cli

        help_text = cli.build_parser().format_help()
        self.assertIn("service", help_text)
        for verb in ("install", "uninstall", "start", "stop", "status", "run"):
            args = cli.build_parser().parse_args(["service", verb])
            self.assertEqual(args.verb, verb)

    def test_service_rejects_unknown_verb(self):
        from src.worker_agent import cli

        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            cli.build_parser().parse_args(["service", "restart"])


class WorkerServiceCommandWindowsTests(unittest.TestCase):
    def setUp(self):
        platform_patch = patch("sys.platform", "win32")
        platform_patch.start()
        self.addCleanup(platform_patch.stop)

    def test_install_prints_registered_command_line(self):
        with patch(
            "src.worker_agent.windows_service.install_service",
            return_value={"name": "VaultlineWorker", "action": "installed", "command_line": '"C:\\w.exe" service run'},
        ) as install:
            exit_code, stdout, _ = _run_cli(["service", "install"])

        install.assert_called_once_with()
        self.assertEqual(exit_code, 0)
        self.assertIn("VaultlineWorker installed", stdout)
        self.assertIn('"C:\\w.exe" service run', stdout)

    def test_uninstall_reports_action(self):
        with patch(
            "src.worker_agent.windows_service.uninstall_service",
            return_value={"name": "VaultlineWorker", "action": "removed"},
        ) as uninstall:
            exit_code, stdout, _ = _run_cli(["service", "uninstall"])

        uninstall.assert_called_once_with()
        self.assertEqual(exit_code, 0)
        self.assertIn("VaultlineWorker removed", stdout)

    def test_start_stop_status_print_status_json(self):
        status = {"name": "VaultlineWorker", "installed": True, "state": "running", "start_type": "auto"}
        for verb, target in (("start", "start_service"), ("stop", "stop_service"), ("status", "service_status")):
            with self.subTest(verb=verb), patch(f"src.worker_agent.windows_service.{target}", return_value=status) as call:
                exit_code, stdout, _ = _run_cli(["service", verb])

            call.assert_called_once_with()
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(stdout), status)

    def test_status_not_installed_exits_zero(self):
        status = {"name": "VaultlineWorker", "installed": False, "state": "not_installed", "start_type": None}
        with patch("src.worker_agent.windows_service.service_status", return_value=status):
            exit_code, stdout, _ = _run_cli(["service", "status"])

        self.assertEqual(exit_code, 0)
        self.assertFalse(json.loads(stdout)["installed"])

    def test_run_dispatches_to_scm_host(self):
        with patch("src.worker_agent.windows_service.run_as_service") as run_as_service:
            exit_code, _, _ = _run_cli(["service", "run"])

        run_as_service.assert_called_once_with()
        self.assertEqual(exit_code, 0)

    def test_run_from_console_hints_daemon_without_traceback(self):
        with patch(
            "src.worker_agent.windows_service.run_as_service",
            side_effect=ServiceError("The service process could not connect to the service controller.", 1063),
        ):
            exit_code, stdout, stderr = _run_cli(["service", "run"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("daemon", stderr)
        self.assertIn("Service Control Manager", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_access_denied_maps_to_elevation_hint(self):
        with patch(
            "src.worker_agent.windows_service.install_service",
            side_effect=ServiceError("Access is denied. (win32 error 5)", 5),
        ):
            exit_code, _, stderr = _run_cli(["service", "install"])

        self.assertEqual(exit_code, 1)
        self.assertIn("run from an elevated PowerShell", stderr)
        self.assertEqual(len(stderr.strip().splitlines()), 1)

    def test_not_installed_error_maps_to_install_hint(self):
        with patch(
            "src.worker_agent.windows_service.start_service",
            side_effect=ServiceError("The specified service does not exist. (win32 error 1060)", 1060),
        ):
            exit_code, _, stderr = _run_cli(["service", "start"])

        self.assertEqual(exit_code, 1)
        self.assertIn("not installed", stderr)
        self.assertIn("service install", stderr)

    def test_other_scm_errors_are_one_line(self):
        with patch(
            "src.worker_agent.windows_service.stop_service",
            side_effect=ServiceError("The service did not respond to the start or control request in a timely fashion. (win32 error 1053)", 1053),
        ):
            exit_code, _, stderr = _run_cli(["service", "stop"])

        self.assertEqual(exit_code, 1)
        self.assertIn("1053", stderr)
        self.assertEqual(len(stderr.strip().splitlines()), 1)

    def test_missing_pywin32_is_reported_clearly(self):
        with patch(
            "src.worker_agent.windows_service.service_status",
            side_effect=ImportError("No module named 'win32serviceutil'"),
        ):
            exit_code, _, stderr = _run_cli(["service", "status"])

        self.assertEqual(exit_code, 1)
        self.assertIn("pywin32", stderr)


class WorkerServiceCommandLinuxTests(unittest.TestCase):
    UNIT = "docker-volume-backup-worker.service"

    def setUp(self):
        platform_patch = patch("sys.platform", "linux")
        platform_patch.start()
        self.addCleanup(platform_patch.stop)

    def test_install_uninstall_run_explain_debian_package_manages_unit(self):
        for verb in ("install", "uninstall", "run"):
            with self.subTest(verb=verb), patch("src.worker_agent.cli.subprocess.run") as run:
                exit_code, _, stderr = _run_cli(["service", verb])

            run.assert_not_called()
            self.assertEqual(exit_code, 2)
            self.assertIn(self.UNIT, stderr)
            self.assertIn("Debian package", stderr)

    def test_start_and_stop_delegate_to_systemctl(self):
        for verb in ("start", "stop"):
            with self.subTest(verb=verb), patch("src.worker_agent.cli.shutil.which", return_value="/usr/bin/systemctl"), patch(
                "src.worker_agent.cli.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0),
            ) as run:
                exit_code, _, _ = _run_cli(["service", verb])

            run.assert_called_once_with(["/usr/bin/systemctl", verb, self.UNIT])
            self.assertEqual(exit_code, 0)

    def test_start_propagates_systemctl_exit_code(self):
        with patch("src.worker_agent.cli.shutil.which", return_value="/usr/bin/systemctl"), patch(
            "src.worker_agent.cli.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=5),
        ):
            exit_code, _, _ = _run_cli(["service", "start"])

        self.assertEqual(exit_code, 5)

    def test_status_reports_active_and_enabled_as_json_even_when_inactive(self):
        def fake_run(command, **_kwargs):
            outputs = {"is-active": "inactive\n", "is-enabled": "enabled\n"}
            return subprocess.CompletedProcess(args=command, returncode=3, stdout=outputs[command[1]], stderr="")

        with patch("src.worker_agent.cli.shutil.which", return_value="/usr/bin/systemctl"), patch(
            "src.worker_agent.cli.subprocess.run", side_effect=fake_run
        ) as run:
            exit_code, stdout, _ = _run_cli(["service", "status"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout), {"name": self.UNIT, "active": "inactive", "enabled": "enabled"})
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["/usr/bin/systemctl", "is-active", self.UNIT], ["/usr/bin/systemctl", "is-enabled", self.UNIT]],
        )

    def test_missing_systemctl_is_reported(self):
        with patch("src.worker_agent.cli.shutil.which", return_value=None), patch("src.worker_agent.cli.subprocess.run") as run:
            exit_code, _, stderr = _run_cli(["service", "status"])

        run.assert_not_called()
        self.assertEqual(exit_code, 1)
        self.assertIn("systemctl", stderr)


if __name__ == "__main__":
    unittest.main()
