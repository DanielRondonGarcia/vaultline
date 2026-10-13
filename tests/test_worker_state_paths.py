import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from src.security.hmac_protocol import digest_secret
from src.worker_agent import state_paths
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.security.credential_store import (
    StoredWorkerCredential,
    WorkerCredentialStore,
)


def _frozen():
    return patch.object(sys, "frozen", True, create=True)


class StatePathResolutionTests(unittest.TestCase):
    def test_env_state_dir_wins_over_platform_defaults(self):
        with patch.dict(os.environ, {"WORKER_STATE_DIR": "/custom/state"}, clear=True), _frozen():
            self.assertEqual(state_paths.resolve_state_dir(), Path("/custom/state"))
            self.assertEqual(state_paths.resolve_credential_file(), Path("/custom/state") / "worker_credentials.json")
            self.assertEqual(state_paths.resolve_enrollment_config_file(), Path("/custom/state") / "worker.json")

    def test_developer_mode_uses_cwd_and_legacy_hidden_credential_file(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "frozen", False, create=True):
            self.assertEqual(state_paths.resolve_state_dir(), Path.cwd())
            self.assertEqual(state_paths.resolve_credential_file(), Path(".worker_credentials.json"))
            self.assertEqual(state_paths.resolve_enrollment_config_file(), Path.cwd() / "worker.json")

    def test_developer_mode_with_state_dir_env_places_credential_inside_it(self):
        with patch.dict(os.environ, {"WORKER_STATE_DIR": "dev-state"}, clear=True), patch.object(sys, "frozen", False, create=True):
            self.assertEqual(state_paths.resolve_credential_file(), Path("dev-state") / "worker_credentials.json")

    def test_credential_file_env_keeps_precedence(self):
        with patch.dict(os.environ, {"WORKER_STATE_DIR": "/custom/state", "WORKER_CREDENTIAL_FILE": "/elsewhere/c.json"}, clear=True):
            self.assertEqual(state_paths.resolve_credential_file(), Path("/elsewhere/c.json"))
            self.assertEqual(state_paths.resolve_enrollment_config_file(), Path("/custom/state") / "worker.json")

    def test_explicit_credential_file_without_state_dir_keeps_worker_json_beside_it(self):
        with patch.dict(os.environ, {"WORKER_CREDENTIAL_FILE": "/elsewhere/c.json"}, clear=True), patch.object(sys, "frozen", False, create=True):
            self.assertEqual(state_paths.resolve_enrollment_config_file(), Path("/elsewhere/worker.json"))

    def test_frozen_windows_default_uses_program_data(self):
        with patch.dict(os.environ, {"ProgramData": r"D:\PD"}, clear=True), _frozen(), patch.object(state_paths, "_platform_is_windows", return_value=True):
            self.assertEqual(state_paths.resolve_state_dir(), Path(r"D:\PD") / "Vaultline" / "worker")
            self.assertEqual(state_paths.resolve_credential_file(), Path(r"D:\PD") / "Vaultline" / "worker" / "worker_credentials.json")
        with patch.dict(os.environ, {}, clear=True), _frozen(), patch.object(state_paths, "_platform_is_windows", return_value=True):
            self.assertEqual(state_paths.resolve_state_dir(), Path(r"C:\ProgramData") / "Vaultline" / "worker")

    def test_frozen_linux_default_uses_var_lib(self):
        with patch.dict(os.environ, {}, clear=True), _frozen(), patch.object(state_paths, "_platform_is_windows", return_value=False):
            self.assertEqual(state_paths.resolve_state_dir(), Path("/var/lib/docker-volume-backup"))
            self.assertEqual(state_paths.resolve_credential_file(), Path("/var/lib/docker-volume-backup/worker_credentials.json"))

    def test_load_enrollment_config_ignores_missing_and_invalid_files(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "worker.json"
            self.assertEqual(state_paths.load_enrollment_config(missing), {})
            missing.write_text("{not json", encoding="utf-8")
            with self.assertLogs("src.worker_agent.state_paths", level="WARNING") as logs:
                self.assertEqual(state_paths.load_enrollment_config(missing), {})
            self.assertTrue(any("worker.json" in line or "enrollment config" in line for line in logs.output))
            missing.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
            self.assertEqual(state_paths.load_enrollment_config(missing), {})

    def test_state_dir_is_managed_only_with_env_or_frozen_executable(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "frozen", False, create=True):
            self.assertFalse(state_paths.state_dir_is_managed())
        with patch.dict(os.environ, {"WORKER_STATE_DIR": "managed"}, clear=True), patch.object(sys, "frozen", False, create=True):
            self.assertTrue(state_paths.state_dir_is_managed())
        with patch.dict(os.environ, {}, clear=True), _frozen():
            self.assertTrue(state_paths.state_dir_is_managed())


class SecureStateDirTests(unittest.TestCase):
    def test_windows_resets_inheritance_and_grants_system_and_administrators_full_control(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(state_paths, "_platform_is_windows", return_value=True), \
                patch.object(state_paths.subprocess, "run") as run:
            state_paths.secure_state_dir(directory)
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["icacls", str(Path(directory)), "/inheritance:r"])
        grants = [args[index + 1] for index, token in enumerate(args) if token == "/grant:r"]
        self.assertEqual(grants, ["*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"])
        self.assertTrue(run.call_args.kwargs.get("check"))

    def test_windows_icacls_failure_raises_permission_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(state_paths, "_platform_is_windows", return_value=True), \
                patch.object(state_paths.subprocess, "run", side_effect=OSError("icacls missing")):
            with self.assertRaises(PermissionError) as raised:
                state_paths.secure_state_dir(directory)
        self.assertIn(str(Path(directory)), str(raised.exception))

    def test_posix_chmods_directory_to_owner_only(self):
        chmods = []
        with tempfile.TemporaryDirectory() as directory, patch.object(state_paths, "_platform_is_windows", return_value=False), \
                patch.object(state_paths.os, "chmod", side_effect=lambda *a, **k: chmods.append(a)), patch.object(state_paths.subprocess, "run") as run:
            state_paths.secure_state_dir(directory)
        run.assert_not_called()
        self.assertEqual(chmods, [(Path(directory), 0o700)])

    def test_posix_chmod_failure_raises_permission_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(state_paths, "_platform_is_windows", return_value=False), \
                patch.object(state_paths.os, "chmod", side_effect=PermissionError("read-only")):
            with self.assertRaises(PermissionError):
                state_paths.secure_state_dir(directory)

    def test_missing_directory_is_rejected_before_touching_acls(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(state_paths.subprocess, "run") as run:
            with self.assertRaises(FileNotFoundError):
                state_paths.secure_state_dir(Path(directory) / "absent")
        run.assert_not_called()


class EnrollmentFilesTrustedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))
        self.credential = Path(self.directory) / "worker_credentials.json"
        self.config = Path(self.directory) / "worker.json"
        self.credential.write_text("{}", encoding="utf-8")
        self.config.write_text("{}", encoding="utf-8")

    def test_system_and_administrators_owners_are_trusted_on_windows(self):
        for sid in ("S-1-5-18", "S-1-5-32-544"):
            with self.subTest(sid=sid):
                self.assertTrue(state_paths.enrollment_files_trusted([self.credential, self.config], owner_resolver=lambda _path: sid))

    def test_standard_user_owner_is_rejected(self):
        self.assertFalse(
            state_paths.enrollment_files_trusted([self.credential, self.config], owner_resolver=lambda _path: "S-1-5-21-1-2-3-1001")
        )

    def test_one_untrusted_file_is_enough_to_reject(self):
        owners = {self.credential: "S-1-5-18", self.config: "S-1-5-21-1-2-3-1001"}
        self.assertFalse(state_paths.enrollment_files_trusted([self.credential, self.config], owner_resolver=lambda path: owners[Path(path)]))

    def test_posix_root_and_current_user_are_trusted_but_other_uids_are_not(self):
        with patch.object(state_paths, "_current_uid", return_value=1000):
            self.assertTrue(state_paths.enrollment_files_trusted([self.credential], owner_resolver=lambda _path: "uid:0"))
            self.assertTrue(state_paths.enrollment_files_trusted([self.credential], owner_resolver=lambda _path: "uid:1000"))
            self.assertFalse(state_paths.enrollment_files_trusted([self.credential], owner_resolver=lambda _path: "uid:1001"))

    def test_missing_files_are_skipped(self):
        calls = []
        absent = Path(self.directory) / "absent.json"
        self.assertTrue(state_paths.enrollment_files_trusted([absent], owner_resolver=lambda path: calls.append(path) or "uid:1001"))
        self.assertEqual(calls, [])

    def test_unknown_owner_is_skipped_with_a_single_warning(self):
        state_paths._reset_owner_check_warning()
        with self.assertLogs("src.worker_agent.state_paths", level="WARNING") as logs:
            self.assertFalse(state_paths.enrollment_files_trusted([self.credential, self.config], owner_resolver=lambda _path: None))
            self.assertFalse(state_paths.enrollment_files_trusted([self.credential], owner_resolver=lambda _path: None))
        self.assertEqual(sum("owner" in line for line in logs.output), 1)

    def test_owner_resolution_error_is_treated_as_untrusted(self):
        def broken(_path):
            raise OSError("access denied")

        self.assertFalse(state_paths.enrollment_files_trusted([self.credential], owner_resolver=broken))

    def test_posix_default_resolver_reports_uid(self):
        fake_stat = types.SimpleNamespace(st_uid=4242)
        with patch.object(state_paths, "_platform_is_windows", return_value=False), patch.object(state_paths.os, "stat", return_value=fake_stat):
            self.assertEqual(state_paths._default_owner_resolver()(self.credential), "uid:4242")

    def test_windows_default_resolver_uses_pywin32_owner_sid(self):
        win32security = types.ModuleType("win32security")
        win32security.OWNER_SECURITY_INFORMATION = 1
        descriptor = Mock()
        descriptor.GetSecurityDescriptorOwner.return_value = "sid-object"
        win32security.GetFileSecurity = Mock(return_value=descriptor)
        win32security.ConvertSidToStringSid = Mock(return_value="S-1-5-18")
        with patch.object(state_paths, "_platform_is_windows", return_value=True), patch.dict(sys.modules, {"win32security": win32security}):
            self.assertEqual(state_paths._default_owner_resolver()(self.credential), "S-1-5-18")
        win32security.GetFileSecurity.assert_called_once_with(str(self.credential), 1)
        win32security.ConvertSidToStringSid.assert_called_once_with("sid-object")

    def test_windows_default_resolver_without_pywin32_returns_unknown(self):
        with patch.object(state_paths, "_platform_is_windows", return_value=True), patch.dict(sys.modules, {"win32security": None}):
            self.assertIsNone(state_paths._default_owner_resolver()(self.credential))


class EnrollPersistsConfigTests(unittest.TestCase):
    class FakeClient:
        instances = []

        def __init__(self, base_url, **kwargs):
            self.base_url = base_url
            self.kwargs = kwargs
            self.store = kwargs["credential_store"]
            type(self).instances.append(self)

        def complete_worker_enrollment_v2(self, bootstrap_secret, attempt_id, credential_secret, labels=None):
            self.store.save("worker-cfg", credential_secret, "1")
            return {"worker_id": "worker-cfg", "credential_version": "1"}

    def setUp(self):
        self.FakeClient.instances = []

    def _run(self, argv, env, secure_state_dir=None):
        from src.worker_agent import cli

        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch("getpass.getpass", return_value="b" * 40), \
                patch("src.worker_agent.cli.is_user_admin", return_value=True), \
                patch("src.worker_agent.cli.ControlPlaneClient", side_effect=self.FakeClient), \
                patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run"), \
                patch("src.worker_agent.cli.secure_state_dir", side_effect=secure_state_dir) as secured, \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        self.secured = secured
        return code, out.getvalue(), err.getvalue()

    def test_enroll_locks_down_managed_state_dir_before_writing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "fresh"
            order = []

            def record(path):
                order.append(("secure", Path(path), (Path(path) / "worker_credentials.json").exists()))

            code, _, err = self._run(["enroll", "--control-plane-url", "http://cp"], {"WORKER_STATE_DIR": str(state_dir)}, secure_state_dir=record)
            self.assertEqual(code, 0, err)
            self.assertEqual(order, [("secure", state_dir, False)])
            self.assertTrue(state_dir.is_dir())

    def test_enroll_in_developer_mode_leaves_cwd_permissions_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "c.json"
            with patch.object(sys, "frozen", False, create=True):
                code, _, err = self._run(["enroll", "--control-plane-url", "http://cp"], {"WORKER_CREDENTIAL_FILE": str(credential)})
            self.assertEqual(code, 0, err)
            self.secured.assert_not_called()

    def test_enroll_without_permission_to_lock_down_state_dir_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            def deny(path):
                raise PermissionError(f"could not restrict permissions on {path}")

            code, out, err = self._run(["enroll", "--control-plane-url", "http://cp"], {"WORKER_STATE_DIR": directory}, secure_state_dir=deny)
            self.assertEqual(code, 1)
            self.assertEqual(self.FakeClient.instances, [])
            self.assertEqual(out, "")
            self.assertEqual(len(err.strip().splitlines()), 1)
            self.assertIn("administrator", err.lower())
            self.assertNotIn("Traceback", err)
            self.assertFalse((Path(directory) / "worker_credentials.json").exists())
            self.assertFalse(list(Path(directory).glob(".*.pending.json")))

    def test_enroll_flags_persist_worker_json_in_state_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            ca_file = os.path.join(directory, "ca.pem")
            code, out, err = self._run(
                ["enroll", "--control-plane-url", "https://cp.example:8443/", "--ca-file", ca_file, "--worker-name", "edge-01"],
                {"WORKER_STATE_DIR": directory},
            )
            self.assertEqual(code, 0, err)
            config_path = Path(directory) / "worker.json"
            self.assertTrue(config_path.exists())
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload,
                {"control_plane_url": "https://cp.example:8443/", "control_plane_ca_file": ca_file, "worker_name": "edge-01"},
            )
            self.assertEqual(self.FakeClient.instances[0].base_url, "https://cp.example:8443/")
            self.assertEqual(self.FakeClient.instances[0].kwargs["ca_file"], ca_file)
            self.assertTrue((Path(directory) / "worker_credentials.json").exists())

    def test_enroll_env_url_is_persisted_without_optional_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _, err = self._run(["enroll"], {"WORKER_STATE_DIR": directory, "CONTROL_PLANE_URL": "http://cp.env"})
            self.assertEqual(code, 0, err)
            payload = json.loads((Path(directory) / "worker.json").read_text(encoding="utf-8"))
            self.assertEqual(payload, {"control_plane_url": "http://cp.env"})

    def test_enroll_requires_control_plane_url_before_prompting(self):
        from src.worker_agent import cli

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"WORKER_STATE_DIR": directory}, clear=True), \
                patch("getpass.getpass") as prompt, redirect_stderr(io.StringIO()) as err:
            code = cli.main(["enroll"])
        self.assertEqual(code, 2)
        prompt.assert_not_called()
        self.assertIn("--control-plane-url", err.getvalue())

    def test_enroll_failure_does_not_write_worker_json(self):
        class FailingClient(self.FakeClient):
            def complete_worker_enrollment_v2(self, *args, **kwargs):
                raise OSError("down")

        from src.worker_agent import cli

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"WORKER_STATE_DIR": directory}, clear=True), \
                patch("getpass.getpass", return_value="b" * 40), patch("src.worker_agent.cli.ControlPlaneClient", side_effect=FailingClient), \
                patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run"), \
                redirect_stderr(io.StringIO()):
            code = cli.main(["enroll", "--control-plane-url", "http://cp"])
        self.assertEqual(code, 1)
        self.assertFalse((Path(directory) / "worker.json").exists())

    def test_self_check_reports_state_dir_and_enrollment_config_booleans(self):
        from src.worker_agent import cli

        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "worker.json").write_text('{"control_plane_url": "https://hidden.example"}', encoding="utf-8")
            with patch.dict(os.environ, {"WORKER_STATE_DIR": directory}, clear=True), patch(
                "src.worker_agent.infrastructure.adapters.native_runtime.NativeRuntimeAdapter.self_check", return_value={}
            ), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(["self-check"]), 0)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["configuration"]["state_dir_configured"])
        self.assertTrue(payload["configuration"]["enrollment_config_exists"])
        self.assertNotIn(directory, output.getvalue())
        self.assertNotIn("hidden.example", output.getvalue())


class BuildServiceConfigFallbackTests(unittest.TestCase):
    def _build(self, env):
        from src.worker_agent import main as worker_main

        with patch.object(worker_main, "DockerRuntimeAdapter", return_value=Mock()), patch.object(
            worker_main.RedisSnapshotCache, "from_env", return_value=None
        ), patch.dict(os.environ, env, clear=True):
            return worker_main.build_service()

    def test_worker_json_is_used_when_env_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "worker.json").write_text(
                json.dumps({"control_plane_url": "https://cp.file", "control_plane_ca_file": "/ca/file.pem", "worker_name": "from-file"}),
                encoding="utf-8",
            )
            service = self._build({"WORKER_STATE_DIR": directory})
            self.assertEqual(service.config.control_plane_url, "https://cp.file")
            self.assertEqual(service.config.name, "from-file")
            self.assertEqual(service.control_plane_client.ca_file, "/ca/file.pem")
            self.assertEqual(service.control_plane_client.credential_store.path, Path(directory) / "worker_credentials.json")
            self.assertEqual(service.recovery_journal.path, Path(directory) / "worker_job_recovery.json")

    def test_environment_wins_over_worker_json(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "worker.json").write_text(json.dumps({"control_plane_url": "https://cp.file", "worker_name": "from-file"}), encoding="utf-8")
            service = self._build({"WORKER_STATE_DIR": directory, "CONTROL_PLANE_URL": "https://cp.env", "WORKER_NAME": "from-env"})
            self.assertEqual(service.config.control_plane_url, "https://cp.env")
            self.assertEqual(service.config.name, "from-env")

    def test_default_url_without_file_or_env(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self._build({"WORKER_STATE_DIR": directory})
            self.assertEqual(service.config.control_plane_url, "http://127.0.0.1:8080")


class _FakeStore:
    def __init__(self):
        self.credential = None
        self.path = Path("ignored")

    def load(self):
        return self.credential


class _FakeClient:
    def __init__(self, store, base_url="http://127.0.0.1:8080", ca_file=None):
        self.credential_store = store
        self.worker_id = None
        self.base_url = base_url
        self.ca_file = ca_file
        self.reconfigure_calls = []

    def reconfigure(self, base_url=None, ca_file=None):
        self.reconfigure_calls.append((base_url, ca_file))
        if base_url:
            self.base_url = base_url.rstrip("/")
        if ca_file is not None:
            self.ca_file = ca_file


class _FakeService:
    def __init__(self, store, enrollment_token=None, control_plane_url="http://cp", name="worker"):
        self.config = WorkerAgentConfig(control_plane_url, name, "host", enrollment_token=enrollment_token)
        self.control_plane_client = _FakeClient(store, base_url=control_plane_url)
        self.runtime = types.SimpleNamespace(runtime_kind="native")
        self.ensure_registered_calls = 0

    def ensure_registered(self):
        self.ensure_registered_calls += 1
        if not self.config.worker_id:
            raise RuntimeError("worker enrollment secret is not configured")
        return self.config.worker_id

    def cleanup_orphaned_runtime_containers(self):
        return {}

    def send_heartbeat(self):
        return None

    def sync_inventory(self):
        return None

    def poll_once(self):
        return []


class IdleUntilEnrolledTests(unittest.TestCase):
    def setUp(self):
        from src.worker_agent import main as worker_main

        self.worker_main = worker_main
        worker_main.reset_stop()
        self.addCleanup(worker_main.reset_stop)

    def _env(self, run_once):
        return patch.dict(
            os.environ,
            {"WORKER_RUN_ONCE": "true" if run_once else "false", "WORKER_HEALTH_PORT": "0", "WORKER_POLL_INTERVAL_SECONDS": "1"},
            clear=True,
        )

    def test_daemon_idles_and_picks_up_credential_without_restart(self):
        store = _FakeStore()
        service = _FakeService(store)
        states = []
        waits = []

        def fake_wait(delay):
            waits.append(delay)
            if len(waits) == 2:
                store.credential = StoredWorkerCredential("worker-late", "s" * 32, "4")
            if len(waits) >= 4:
                self.worker_main.request_stop()
            return self.worker_main._STOP_EVENT.is_set()

        health_cls = self.worker_main.WorkerHealthState
        captured = {}

        def capture_state(*args, **kwargs):
            state = health_cls(*args, **kwargs)
            captured["state"] = state
            original = state.record_not_ready

            def record(error):
                states.append(("not_ready", error))
                original(error)

            state.record_not_ready = record
            return state

        with self._env(run_once=False), patch.object(self.worker_main, "build_service", return_value=service), \
                patch.object(self.worker_main, "start_health_server"), patch.object(self.worker_main, "WorkerHealthState", side_effect=capture_state), \
                patch.object(self.worker_main, "_wait_for_stop", side_effect=fake_wait), self.assertLogs(self.worker_main.logger, level="INFO") as logs:
            self.worker_main.main()

        self.assertEqual(states[0][0], "not_ready")
        self.assertIn("enrollment", states[0][1])
        self.assertEqual(waits[:2], [1.0, 1.0])
        self.assertEqual(service.config.worker_id, "worker-late")
        self.assertEqual(service.control_plane_client.worker_id, "worker-late")
        self.assertGreaterEqual(service.ensure_registered_calls, 1)
        self.assertEqual(captured["state"].snapshot()["status"], "ok")
        self.assertTrue(any("vaultline-worker enroll --control-plane-url" in line for line in logs.output))
        self.assertEqual(sum("Waiting for enrollment" in line for line in logs.output), 1)

    def _run_transition(self, directory, env_overrides, enroll_payload, trusted=True, max_waits=2):
        """Start unenrolled, write worker.json + credential during the idle wait, stop after one registered loop."""
        store = _FakeStore()
        store.path = Path(directory) / "worker_credentials.json"
        service = _FakeService(store, control_plane_url="http://127.0.0.1:8080")
        health = {}
        waits = []

        def fake_wait(delay):
            waits.append(delay)
            if store.credential is None:
                (Path(directory) / "worker.json").write_text(json.dumps(enroll_payload), encoding="utf-8")
                store.path.write_text("{}", encoding="utf-8")
                store.credential = StoredWorkerCredential("worker-late", "s" * 32, "1")
                return False
            if len(waits) >= max_waits:
                self.worker_main.request_stop()
                return True
            return False

        health_cls = self.worker_main.WorkerHealthState

        def capture_state(*args, **kwargs):
            health["state"] = health_cls(*args, **kwargs)
            return health["state"]

        env = {"WORKER_RUN_ONCE": "false", "WORKER_HEALTH_PORT": "0", "WORKER_POLL_INTERVAL_SECONDS": "1", "WORKER_STATE_DIR": directory}
        env.update(env_overrides)
        with patch.dict(os.environ, env, clear=True), patch.object(self.worker_main, "build_service", return_value=service), \
                patch.object(self.worker_main, "start_health_server"), patch.object(self.worker_main, "WorkerHealthState", side_effect=capture_state), \
                patch.object(self.worker_main, "enrollment_files_trusted", return_value=trusted) as trust_check, \
                patch.object(self.worker_main, "_wait_for_stop", side_effect=fake_wait), self.assertLogs(self.worker_main.logger, level="INFO") as logs:
            self.worker_main.main()
        self.trust_check = trust_check
        return service, health["state"], logs.output

    def test_runtime_enrollment_is_adopted_only_after_both_files_pass_the_owner_check(self):
        with tempfile.TemporaryDirectory() as directory:
            service, _, _ = self._run_transition(directory, {}, {"control_plane_url": "https://cp.enrolled"})
        self.assertEqual(service.config.worker_id, "worker-late")
        checked = [Path(p) for p in self.trust_check.call_args.args[0]]
        self.assertEqual(checked, [Path(directory) / "worker_credentials.json", Path(directory) / "worker.json"])

    def test_untrusted_enrollment_files_are_refused_and_worker_stays_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            service, state, logs = self._run_transition(directory, {}, {"control_plane_url": "https://cp.attacker"}, trusted=False, max_waits=4)
        self.assertIsNone(service.config.worker_id)
        self.assertIsNone(service.control_plane_client.worker_id)
        self.assertEqual(service.ensure_registered_calls, 0)
        self.assertEqual(service.config.control_plane_url, "http://127.0.0.1:8080")
        self.assertEqual(service.control_plane_client.reconfigure_calls, [])
        self.assertEqual(state.snapshot()["status"], "not_ready")
        refusals = [line for line in logs if "refusing untrusted enrollment files" in line]
        self.assertEqual(len(refusals), 1)
        self.assertIn(directory, refusals[0])
        self.assertTrue(refusals[0].startswith("ERROR:"))
        self.assertNotIn("Enrollment detected", "\n".join(logs))

    def test_enrollment_detected_at_runtime_applies_worker_json_connection_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            ca_file = os.path.join(directory, "ca.pem")
            service, state, logs = self._run_transition(
                directory,
                {},
                {"control_plane_url": "https://cp.enrolled:8443/", "control_plane_ca_file": ca_file, "worker_name": "edge-late"},
            )
        client = service.control_plane_client
        self.assertEqual(service.config.control_plane_url, "https://cp.enrolled:8443/")
        self.assertEqual(service.config.name, "edge-late")
        self.assertEqual(client.base_url, "https://cp.enrolled:8443")
        self.assertEqual(client.ca_file, ca_file)
        self.assertEqual(client.reconfigure_calls, [("https://cp.enrolled:8443/", ca_file)])
        self.assertEqual(client.worker_id, "worker-late")
        self.assertEqual(service.ensure_registered_calls, 1)
        snapshot = state.snapshot()
        self.assertEqual(snapshot["control_plane_url"], "https://cp.enrolled:8443/")
        self.assertEqual(snapshot["worker_name"], "edge-late")
        self.assertEqual(snapshot["status"], "ok")
        detected = [line for line in logs if "Enrollment detected" in line]
        self.assertEqual(len(detected), 1)
        self.assertIn("https://cp.enrolled:8443/", detected[0])
        self.assertNotIn("s" * 32, "\n".join(logs))

    def test_environment_still_wins_after_runtime_enrollment(self):
        with tempfile.TemporaryDirectory() as directory:
            service, state, _ = self._run_transition(
                directory,
                {"CONTROL_PLANE_URL": "http://127.0.0.1:8080", "WORKER_NAME": "worker", "CONTROL_PLANE_CA_FILE": "/env/ca.pem"},
                {"control_plane_url": "https://cp.enrolled", "control_plane_ca_file": "/file/ca.pem", "worker_name": "edge-late"},
            )
        client = service.control_plane_client
        self.assertEqual(service.config.control_plane_url, "http://127.0.0.1:8080")
        self.assertEqual(service.config.name, "worker")
        self.assertEqual(client.base_url, "http://127.0.0.1:8080")
        self.assertIsNone(client.ca_file)
        self.assertEqual(client.reconfigure_calls, [])
        self.assertEqual(state.snapshot()["control_plane_url"], "http://127.0.0.1:8080")
        self.assertEqual(service.ensure_registered_calls, 1)

    def test_control_plane_client_reconfigure_updates_request_fields(self):
        from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient

        client = ControlPlaneClient("http://old", ca_file=None)
        client.reconfigure(base_url="https://new.example/", ca_file="/ca.pem")
        self.assertEqual(client.base_url, "https://new.example")
        self.assertEqual(client.ca_file, "/ca.pem")
        client.reconfigure(base_url=None, ca_file=None)
        self.assertEqual(client.base_url, "https://new.example")
        self.assertEqual(client.ca_file, "/ca.pem")

    def test_run_once_without_enrollment_raises_without_consuming_attempts(self):
        service = _FakeService(_FakeStore())
        with self._env(run_once=True), patch.object(self.worker_main, "build_service", return_value=service), \
                patch.object(self.worker_main, "start_health_server"), patch.object(self.worker_main, "_wait_for_stop", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "not enrolled"):
                self.worker_main.main()
        self.assertEqual(service.ensure_registered_calls, 0)

    def test_enrollment_token_present_keeps_real_failure_cap(self):
        service = _FakeService(_FakeStore(), enrollment_token="t" * 32)
        with self._env(run_once=True), patch.object(self.worker_main, "build_service", return_value=service), \
                patch.object(self.worker_main, "start_health_server"), patch.object(self.worker_main, "_wait_for_stop", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "5 attempts"):
                self.worker_main.main()
        self.assertEqual(service.ensure_registered_calls, 5)

    def test_stop_event_ends_registered_loop_cleanly(self):
        store = _FakeStore()
        store.credential = StoredWorkerCredential("worker-ready", "s" * 32, "1")
        service = _FakeService(store)
        loops = []

        def fake_wait(delay):
            loops.append(delay)
            self.worker_main.request_stop()
            return True

        with self._env(run_once=False), patch.object(self.worker_main, "build_service", return_value=service), \
                patch.object(self.worker_main, "start_health_server"), patch.object(self.worker_main, "_wait_for_stop", side_effect=fake_wait):
            self.assertIsNone(self.worker_main.main())
        self.assertEqual(len(loops), 1)
        self.assertEqual(service.ensure_registered_calls, 1)
        self.assertTrue(self.worker_main.stop_requested())

    def test_stop_requested_before_start_returns_immediately(self):
        service = _FakeService(_FakeStore())
        self.worker_main.request_stop()
        with self._env(run_once=False), patch.object(self.worker_main, "build_service", return_value=service), \
                patch.object(self.worker_main, "start_health_server"):
            self.assertIsNone(self.worker_main.main())
        self.assertEqual(service.ensure_registered_calls, 0)

    def test_wait_for_stop_returns_early_when_event_is_set(self):
        timer = threading.Timer(0.05, self.worker_main.request_stop)
        timer.start()
        try:
            self.assertTrue(self.worker_main._wait_for_stop(30))
        finally:
            timer.cancel()


class _OSProxy:
    def __init__(self, name, **overrides):
        self.name = name
        self._overrides = overrides

    def __getattr__(self, attribute):
        if attribute in self._overrides:
            return self._overrides[attribute]
        return getattr(os, attribute)


class CredentialOwnershipTests(unittest.TestCase):
    def test_windows_acl_grants_system_and_administrators_by_sid(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkerCredentialStore(os.path.join(directory, "credentials.json"))
            with patch("src.worker_agent.infrastructure.security.credential_store.os", _OSProxy("nt")), \
                    patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run") as run:
                store.save("worker-acl", "c" * 32, "1")
            args = run.call_args.args[0]
            self.assertEqual(args[:3], ["icacls", str(store.path), "/inheritance:r"])
            grants = [args[index + 1] for index, token in enumerate(args) if token == "/grant:r"]
            self.assertIn("*S-1-5-18:(R,W)", grants)
            self.assertIn("*S-1-5-32-544:(R,W)", grants)
            self.assertEqual(len(grants), 3)

    def test_windows_locks_down_a_managed_state_dir_it_just_created(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "fresh"
            store = WorkerCredentialStore(state_dir / "credentials.json")
            with patch.dict(os.environ, {"WORKER_STATE_DIR": str(state_dir)}), \
                    patch("src.worker_agent.infrastructure.security.credential_store.os", _OSProxy("nt")), \
                    patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run"), \
                    patch("src.worker_agent.infrastructure.security.credential_store.secure_state_dir") as secure:
                store.save("worker-acl", "c" * 32, "1")
                secure.assert_called_once_with(state_dir)
                store.save("worker-acl", "d" * 32, "1")
            # The directory existed on the second write: its ACL is not rewritten.
            self.assertEqual(secure.call_count, 1)

    def test_windows_never_re_acls_an_existing_or_unmanaged_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = WorkerCredentialStore(Path(directory) / "credentials.json")
            unmanaged = WorkerCredentialStore(Path(directory) / "dev" / "credentials.json")
            with patch.dict(os.environ, {}, clear=True), patch.object(sys, "frozen", False, create=True), \
                    patch("src.worker_agent.infrastructure.security.credential_store.os", _OSProxy("nt")), \
                    patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run"), \
                    patch("src.worker_agent.infrastructure.security.credential_store.secure_state_dir") as secure:
                existing.save("worker-a", "c" * 32, "1")
                unmanaged.save("worker-b", "c" * 32, "1")
            secure.assert_not_called()

    def test_windows_does_not_acl_legacy_credential_parent_outside_managed_state_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "managed"
            legacy_parent = Path(directory) / "legacy"
            legacy_parent.mkdir()
            store = WorkerCredentialStore(legacy_parent / "credentials.json")
            with patch.dict(os.environ, {"WORKER_STATE_DIR": str(state_dir)}), \
                    patch("src.worker_agent.infrastructure.security.credential_store.os", _OSProxy("nt")), \
                    patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run"), \
                    patch("src.worker_agent.infrastructure.security.credential_store.secure_state_dir") as secure:
                store.save("worker-legacy", "e" * 32, "1")

            secure.assert_not_called()

    def test_windows_sets_administrators_as_owner_only_for_managed_final_files(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "managed"
            state_dir.mkdir()
            store = WorkerCredentialStore(state_dir / "worker_credentials.json")
            calls = []
            with patch.dict(os.environ, {"WORKER_STATE_DIR": str(state_dir)}), \
                    patch("src.worker_agent.infrastructure.security.credential_store.os", _OSProxy("nt")), \
                    patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run", side_effect=lambda args, **kwargs: calls.append((args, kwargs))), \
                    patch("src.worker_agent.infrastructure.security.credential_store.os.getlogin", return_value="enroller"), \
                    patch("src.worker_agent.infrastructure.security.credential_store.getpass.getuser", return_value="enroller"):
                store.save("worker-managed", "c" * 32, "1")

            owner_calls = [args for args, _ in calls if "/setowner" in args]
            self.assertEqual(len(owner_calls), 1)
            self.assertEqual(owner_calls[0][-2:], ["/setowner", "*S-1-5-32-544"])
            self.assertEqual(owner_calls[0][1], str(state_dir / "worker_credentials.json"))
            self.assertTrue(any("enroller:(R,W)" in arg for args, _ in calls for arg in args))
            self.assertTrue(any("*S-1-5-18:(R,W)" in arg for args, _ in calls for arg in args))
            self.assertTrue(any("*S-1-5-32-544:(R,W)" in arg for args, _ in calls for arg in args))

    def test_windows_does_not_set_administrators_owner_for_unmanaged_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkerCredentialStore(Path(directory) / "worker_credentials.json")
            calls = []
            with patch.dict(os.environ, {}, clear=True), patch.object(sys, "frozen", False, create=True), \
                    patch("src.worker_agent.infrastructure.security.credential_store.os", _OSProxy("nt")), \
                    patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run", side_effect=lambda args, **kwargs: calls.append(args)), \
                    patch("src.worker_agent.infrastructure.security.credential_store.getpass.getuser", return_value="developer"):
                store.save("worker-dev", "d" * 32, "1")
            self.assertFalse(any("/setowner" in args for args in calls))

    def test_posix_root_write_chowns_to_state_dir_owner(self):
        chowns = []
        parent_stat = types.SimpleNamespace(st_uid=1234, st_gid=4321)
        with tempfile.TemporaryDirectory() as directory:
            store = WorkerCredentialStore(os.path.join(directory, "credentials.json"))
            proxy = _OSProxy("posix", geteuid=lambda: 0, stat=lambda path, *a, **k: parent_stat, chown=lambda *a: chowns.append(a), chmod=lambda *a, **k: None)
            with patch("src.worker_agent.infrastructure.security.credential_store.os", proxy):
                store.save("worker-own", "c" * 32, "1")
                store.save_pending_enrollment("attempt-123456789012", "d" * 32, digest_secret("a" * 32))
                store.write_json(Path(directory) / "worker.json", {"control_plane_url": "http://cp"})
            written = json.loads((Path(directory) / "worker.json").read_text(encoding="utf-8"))
        self.assertEqual(written, {"control_plane_url": "http://cp"})
        self.assertEqual(
            chowns,
            [(store.path, 1234, 4321), (store.pending_path, 1234, 4321), (Path(directory) / "worker.json", 1234, 4321)],
        )

    def test_posix_non_root_or_root_owned_parent_does_not_chown(self):
        chowns = []
        with tempfile.TemporaryDirectory() as directory:
            store = WorkerCredentialStore(os.path.join(directory, "credentials.json"))
            non_root = _OSProxy("posix", geteuid=lambda: 1000, chown=lambda *a: chowns.append(a), chmod=lambda *a, **k: None)
            root_parent = _OSProxy(
                "posix", geteuid=lambda: 0, stat=lambda *a, **k: types.SimpleNamespace(st_uid=0, st_gid=0), chown=lambda *a: chowns.append(a), chmod=lambda *a, **k: None
            )
            with patch("src.worker_agent.infrastructure.security.credential_store.os", non_root):
                store.save("worker-a", "c" * 32, "1")
            with patch("src.worker_agent.infrastructure.security.credential_store.os", root_parent):
                store.save("worker-b", "c" * 32, "1")
        self.assertEqual(chowns, [])


if __name__ == "__main__":
    unittest.main()
