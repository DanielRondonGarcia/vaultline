import io
import json
import os
import socket
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from src.security.hmac_protocol import digest_secret
from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore


class _WindowsOSProxy:
    name = "nt"

    def __getattr__(self, attribute):
        return getattr(os, attribute)


class WorkerEnrollmentCLITests(unittest.TestCase):
    def test_pending_creation_race_reloads_existing_attempt_and_rejects_other_token(self):
        from src.worker_agent import cli

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "credentials.json")
            store = WorkerCredentialStore(path)
            existing = ("attempt-racer123456", "r" * 48, digest_secret("a" * 32))
            original_save = store.save_pending_enrollment
            raced_pending = original_save(*existing)
            store.delete_pending_enrollment()

            def competing_create(*_args):
                original_save(*existing)
                raise FileExistsError(store.pending_path)

            errors = io.StringIO()
            with patch.dict(os.environ, {"WORKER_CREDENTIAL_FILE": path}, clear=True), \
                    patch("src.worker_agent.cli.state_dir_is_managed", return_value=False), \
                    patch("getpass.getpass", return_value="b" * 32), \
                    patch("src.worker_agent.cli.WorkerCredentialStore.load_pending_enrollment", side_effect=[None, raced_pending]), \
                    patch("src.worker_agent.cli.WorkerCredentialStore.save_pending_enrollment", side_effect=competing_create), \
                    patch("src.worker_agent.cli.ControlPlaneClient") as client, redirect_stderr(errors):
                code = cli.main(["enroll", "--control-plane-url", "https://example.invalid"])

            self.assertEqual(code, 2)
            self.assertIn("different bootstrap token", errors.getvalue())
            client.assert_not_called()
            saved = store.load_pending_enrollment()
            self.assertEqual((saved.attempt_id, saved.durable_credential, saved.token_digest), existing)

    def test_pending_acl_warning_includes_reset_hint_for_error_codes_5_and_1300(self):
        from src.worker_agent import cli
        from src.worker_agent.infrastructure.security.credential_store import WorkerStateACLPermissionError

        for error_code in (5, 1300):
            with self.subTest(error_code=error_code), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "credentials.json")
                errors = io.StringIO()
                token = "secret-token-that-must-not-leak-0123456789"
                failure = WorkerStateACLPermissionError(
                    "private path and https://secret.invalid", icacls_error_code=error_code,
                    pending_state_may_exist=True,
                )
                with patch("src.worker_agent.cli._is_windows", return_value=True), \
                        patch.dict(os.environ, {"WORKER_CREDENTIAL_FILE": path}, clear=True), \
                        patch("src.worker_agent.cli.state_dir_is_managed", return_value=False), \
                        patch("getpass.getpass", return_value=token), \
                        patch("src.worker_agent.cli.WorkerCredentialStore.save_pending_enrollment", side_effect=failure), \
                        redirect_stderr(errors):
                    code = cli.main(["enroll", "--control-plane-url", "https://control.invalid"])

                rendered = errors.getvalue()
                self.assertEqual(code, 1)
                self.assertEqual(len(rendered.strip().splitlines()), 1)
                self.assertIn("'vaultline-worker enroll --reset-pending'", rendered)
                self.assertNotIn(token, rendered)
                self.assertNotIn(directory, rendered)
                self.assertNotIn("https://", rendered)
                self.assertEqual("icacls error 5" in rendered, error_code == 5)

    def test_managed_windows_enrollment_requires_admin_before_prompt(self):
        from src.worker_agent import cli

        errors = io.StringIO()
        with patch("src.worker_agent.cli._is_windows", return_value=True), \
                patch("src.worker_agent.cli.state_dir_is_managed", return_value=True), \
                patch("src.worker_agent.cli.is_user_admin", return_value=False), \
                patch("getpass.getpass") as prompt, redirect_stderr(errors):
            code = cli.main(["enroll", "--control-plane-url", "https://example.invalid"])

        self.assertEqual(code, 1)
        prompt.assert_not_called()
        self.assertEqual(errors.getvalue().strip(), "error: open PowerShell as Administrator and rerun 'vaultline-worker enroll ...'")

    def test_pending_state_acl_error_after_prompt_is_honest_and_does_not_leak_secrets(self):
        from src.worker_agent import cli

        errors = io.StringIO()
        token = "bootstrap-secret-should-not-appear-123"
        with tempfile.TemporaryDirectory() as directory, \
                patch("src.worker_agent.cli._is_windows", return_value=True), \
                patch.dict(os.environ, {"WORKER_CREDENTIAL_FILE": os.path.join(directory, "credentials.json")}, clear=True), \
                patch("src.worker_agent.cli.state_dir_is_managed", return_value=False), \
                patch("getpass.getpass", return_value=token), \
                patch("src.worker_agent.infrastructure.security.credential_store.os", _WindowsOSProxy()), \
                patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run", side_effect=[None, __import__("subprocess").CalledProcessError(5, "icacls")]), \
                redirect_stderr(errors):
            code = cli.main(["enroll", "--control-plane-url", "https://example.invalid"])

        self.assertEqual(code, 1)
        rendered = errors.getvalue()
        self.assertIn("Windows denied access while securing worker credentials", rendered)
        self.assertIn("icacls error 5", rendered)
        self.assertIn("No credential was enrolled", rendered)
        self.assertNotIn("run as administrator", rendered.lower())
        self.assertNotIn("Traceback", rendered)
        self.assertNotIn(token, rendered)
        self.assertNotIn("https://", rendered)
        self.assertNotIn(directory, rendered)
        self.assertFalse(WorkerCredentialStore(os.path.join(directory, "credentials.json")).pending_path.exists())

    def test_pending_file_remains_retryable_if_it_was_completed_before_network_failure(self):
        sent = []

        class FailingClient:
            def __init__(self, base_url, **kwargs):
                self.store = kwargs["credential_store"]

            def complete_worker_enrollment_v2(self, token, attempt_id, credential, labels=None):
                sent.append((token, attempt_id, credential))
                raise OSError("offline")

        with tempfile.TemporaryDirectory() as directory:
            first = self.run_enroll(directory, client_factory=FailingClient)
            store = WorkerCredentialStore(first[3])
            self.assertEqual(first[0], 1)
            self.assertIsNotNone(store.load_pending_enrollment())
            second = self.run_enroll(directory, client_factory=FailingClient)
            self.assertEqual(second[0], 1)
            self.assertEqual(sent[0], sent[1])
            self.assertIsNotNone(store.load_pending_enrollment())

    def run_enroll(self, directory, token="t" * 32, client_factory=None, argv=None):
        from src.worker_agent import cli

        output = io.StringIO()
        errors = io.StringIO()
        credential_path = os.path.join(directory, "credentials.json")
        env = {
            "CONTROL_PLANE_URL": "http://control-plane.invalid",
            "WORKER_CREDENTIAL_FILE": credential_path,
        }
        patches = [
            patch.dict(os.environ, env, clear=True),
            patch("getpass.getpass", return_value=token),
        ]
        if client_factory is not None:
            patches.append(patch("src.worker_agent.cli.ControlPlaneClient", side_effect=client_factory))
        with patches[0], patches[1]:
            client_patch = patches[2] if len(patches) > 2 else None
            if client_patch:
                with client_patch:
                    with redirect_stdout(output), redirect_stderr(errors):
                        code = cli.main(argv or ["enroll"])
            else:
                with redirect_stdout(output), redirect_stderr(errors):
                    code = cli.main(argv or ["enroll"])
        return code, output.getvalue(), errors.getvalue(), credential_path

    def test_enroll_prompts_hidden_and_pending_exists_before_http_call(self):
        calls = []

        with tempfile.TemporaryDirectory() as directory:
            credential_path = os.path.join(directory, "credentials.json")

            class FakeClient:
                def __init__(self, base_url, **kwargs):
                    self.store = kwargs["credential_store"]

                def complete_worker_enrollment_v2(self, bootstrap_secret, attempt_id, credential_secret, labels=None):
                    pending = self.store.load_pending_enrollment()
                    self.store.save("worker-cli", credential_secret, "3")
                    calls.append((bootstrap_secret, attempt_id, credential_secret, pending))
                    return {"worker_id": "worker-cli", "credential_version": "3"}

            with patch.dict(os.environ, {"CONTROL_PLANE_URL": "http://cp", "WORKER_CREDENTIAL_FILE": credential_path}, clear=True), \
                patch("getpass.getpass", return_value="bootstrap-token-value-1234567890"), \
                patch("src.worker_agent.cli.ControlPlaneClient", side_effect=FakeClient) as client_class, \
                redirect_stdout(io.StringIO()) as output:
                from src.worker_agent import cli
                code = cli.main(["enroll"])

            self.assertEqual(code, 0)
            self.assertEqual(
                output.getvalue(),
                "SUCCESS: Worker enrolled.\n"
                "  Worker ID: worker-cli\n"
                "  Control Plane settings: saved\n"
                "  Credential: stored securely\n"
                "\n"
                "If installed as a service, it will pick up enrollment within the next poll "
                "(default up to 30 seconds).\n"
                "Check status: vaultline-worker service status\n",
            )
            client_class.assert_called_once()
            self.assertEqual(calls[0][0], "bootstrap-token-value-1234567890")
            self.assertEqual(calls[0][3].attempt_id, calls[0][1])
            self.assertEqual(calls[0][3].durable_credential, calls[0][2])
            self.assertEqual(calls[0][3].token_digest, digest_secret("bootstrap-token-value-1234567890"))
            self.assertIsNone(WorkerCredentialStore(credential_path).load_pending_enrollment())

    def test_retry_same_token_reuses_exact_attempt_and_credential_after_network_failure(self):
        sent = []

        class FakeClient:
            def __init__(self, base_url, **kwargs):
                self.store = kwargs["credential_store"]

            def complete_worker_enrollment_v2(self, bootstrap_secret, attempt_id, credential_secret, labels=None):
                sent.append((bootstrap_secret, attempt_id, credential_secret, labels))
                if len(sent) == 1:
                    raise OSError("network unavailable")
                self.store.save("worker-retry", credential_secret, "1")
                return {"worker_id": "worker-retry", "credential_version": "1"}

        with tempfile.TemporaryDirectory() as directory:
            first = self.run_enroll(directory, client_factory=FakeClient)
            self.assertEqual(first[0], 1)
            self.assertIsNotNone(WorkerCredentialStore(first[3]).load_pending_enrollment())
            second = self.run_enroll(directory, client_factory=FakeClient)

        self.assertEqual(second[0], 0)
        self.assertEqual(sent[0], sent[1])

    def test_different_token_with_pending_is_rejected_without_sending_or_leaking_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "credentials.json")
            store = WorkerCredentialStore(path)
            store.save_pending_enrollment("attempt-123456789012", "c" * 32, digest_secret("a" * 32))

            class FakeClient:
                def __init__(self, *args, **kwargs):
                    raise AssertionError("HTTP client must not be created for mismatched pending token")

            code, out, err, _ = self.run_enroll(directory, token="b" * 32, client_factory=FakeClient)
            still_pending = store.load_pending_enrollment()

        rendered = out + err
        self.assertEqual(code, 2)
        self.assertIn("pending enrollment", rendered)
        self.assertIn("'vaultline-worker enroll --reset-pending'", rendered)
        self.assertNotIn("b" * 32, rendered)
        self.assertIsNotNone(still_pending)

    def test_bootstrap_token_flag_and_env_skip_the_hidden_prompt(self):
        sent = []

        class FakeClient:
            def __init__(self, base_url, **kwargs):
                self.store = kwargs["credential_store"]

            def complete_worker_enrollment_v2(self, bootstrap_secret, attempt_id, credential_secret, labels=None):
                sent.append(bootstrap_secret)
                self.store.save("worker-flag", credential_secret, "1")
                return {"worker_id": "worker-flag", "credential_version": "1"}

        from src.worker_agent import cli

        flag_token = "flag-token-value-abcdefghijklmnopqrstuvwxyz"
        env_token = "env-token-value-abcdefghijklmnopqrstuvwxyz"
        for label, argv, env_extra in (
            ("flag", ["enroll", "--bootstrap-token", f"  {flag_token}  "], {}),
            ("env", ["enroll"], {"VAULTLINE_BOOTSTRAP_TOKEN": env_token}),
        ):
            with self.subTest(source=label), tempfile.TemporaryDirectory() as directory:
                env = {"CONTROL_PLANE_URL": "http://cp", "WORKER_CREDENTIAL_FILE": os.path.join(directory, "credentials.json"), **env_extra}
                with patch.dict(os.environ, env, clear=True), patch("getpass.getpass") as prompt, \
                        patch("src.worker_agent.cli.ControlPlaneClient", side_effect=FakeClient), \
                        redirect_stdout(io.StringIO()) as out:
                    code = cli.main(argv)
                self.assertEqual(code, 0)
                prompt.assert_not_called()
                self.assertEqual(sent[-1], flag_token if label == "flag" else env_token)
                self.assertNotIn(sent[-1], out.getvalue())

    def test_hidden_prompt_explains_that_input_is_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            prompts = []

            def record_prompt(prompt):
                prompts.append(prompt)
                return ""

            from src.worker_agent import cli

            with patch.dict(os.environ, {"CONTROL_PLANE_URL": "http://cp", "WORKER_CREDENTIAL_FILE": os.path.join(directory, "credentials.json")}, clear=True), \
                    patch("getpass.getpass", side_effect=record_prompt), redirect_stderr(io.StringIO()):
                cli.main(["enroll"])
        self.assertEqual(prompts, ["Bootstrap token (input is hidden, paste then press Enter): "])

    def test_empty_or_short_token_is_rejected_before_any_pending_state_or_http_call(self):
        from src.worker_agent import cli

        for label, token, expected in (
            ("empty", "", "bootstrap token is empty"),
            ("whitespace", "   \n", "bootstrap token is empty"),
            ("short", "short-token-only-31-bytes-long!", "too short"),
        ):
            with self.subTest(token=label), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "credentials.json")
                errors = io.StringIO()
                with patch.dict(os.environ, {"CONTROL_PLANE_URL": "http://cp", "WORKER_CREDENTIAL_FILE": path}, clear=True), \
                        patch("getpass.getpass", return_value=token), \
                        patch("src.worker_agent.cli.ControlPlaneClient") as client, redirect_stderr(errors):
                    code = cli.main(["enroll"])
                self.assertEqual(code, 2)
                self.assertIn(expected, errors.getvalue())
                client.assert_not_called()
                self.assertFalse(WorkerCredentialStore(path).pending_path.exists())
                self.assertEqual(os.listdir(directory), [])

    def test_failure_prints_redacted_server_detail_and_named_retry_exits(self):
        from src.worker_agent import cli
        from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneHTTPError
        from urllib.error import URLError

        token = "bootstrap-token-value-1234567890"
        cases = (
            (
                ControlPlaneHTTPError("POST", "/api/v2/worker-enrollments/complete", 400, "worker enrollment token expired", "enrollment_expired"),
                "Enrollment failed: Control Plane rejected POST /api/v2/worker-enrollments/complete: HTTP 400 (code=enrollment_expired): worker enrollment token expired",
            ),
            (
                URLError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"),
                "Enrollment failed: URLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate",
            ),
            (
                URLError(socket.gaierror(11001, "getaddrinfo failed")),
                "Enrollment failed: DNS lookup failed for the Control Plane host. Verify the hostname in --control-plane-url and confirm this server can resolve it (DNS/VPN/network). The bootstrap token was not validated.",
            ),
            (TimeoutError(), "Enrollment failed: TimeoutError"),
            (OSError(f"connection to https://cp.invalid/?token={token} refused"), "Enrollment failed: OSError: connection to <url> refused"),
        )
        for failure, expected_first_line in cases:
            class FailingClient:
                def __init__(self, base_url, **kwargs):
                    pass

                def complete_worker_enrollment_v2(self, *args, **kwargs):
                    raise failure

            with self.subTest(failure=failure.__class__.__name__), tempfile.TemporaryDirectory() as directory:
                code, out, err, path = self.run_enroll(directory, token=token, client_factory=FailingClient)
                lines = err.strip().splitlines()
                self.assertEqual(code, 1)
                self.assertEqual(lines[0], expected_first_line)
                self.assertIn("re-run enroll with the same bootstrap token to retry", lines[1])
                self.assertIn("'vaultline-worker enroll --reset-pending'", lines[1])
                self.assertNotIn(token, err)
                self.assertNotIn("cp.invalid", err)
                self.assertNotIn("control-plane.invalid", err)
                self.assertIsNotNone(WorkerCredentialStore(path).load_pending_enrollment())

    def test_missing_ca_file_warns_instead_of_silently_using_system_trust(self):
        class FakeClient:
            def __init__(self, base_url, **kwargs):
                self.store = kwargs["credential_store"]

            def complete_worker_enrollment_v2(self, bootstrap_secret, attempt_id, credential_secret, labels=None):
                self.store.save("worker-ca", credential_secret, "1")
                return {"worker_id": "worker-ca", "credential_version": "1"}

        with tempfile.TemporaryDirectory() as directory:
            missing = os.path.join(directory, "missing-ca.pem")
            present = os.path.join(directory, "ca.pem")
            with open(present, "w", encoding="utf-8") as stream:
                stream.write("-----BEGIN CERTIFICATE-----\n")
            for ca_file, expect_warning in ((missing, True), (present, False)):
                with self.subTest(exists=not expect_warning):
                    code, _, err, _ = self.run_enroll(
                        directory, client_factory=FakeClient, argv=["enroll", "--ca-file", ca_file],
                    )
                    self.assertEqual(code, 0, err)
                    self.assertEqual("warning: --ca-file does not exist" in err, expect_warning)
                    self.assertNotIn(directory, err)
                WorkerCredentialStore(os.path.join(directory, "credentials.json")).path.unlink()

    def test_pending_file_never_contains_plaintext_token_and_is_removed_on_success(self):
        token = "plaintext-token-value-1234567890!!"
        snapshots = []

        class FakeClient:
            def __init__(self, base_url, **kwargs):
                self.store = kwargs["credential_store"]

            def complete_worker_enrollment_v2(self, bootstrap_secret, attempt_id, credential_secret, labels=None):
                snapshots.append(self.store.pending_path.read_text(encoding="utf-8"))
                self.store.save("worker-clean", credential_secret, "1")
                return {"worker_id": "worker-clean", "credential_version": "1"}

        with tempfile.TemporaryDirectory() as directory:
            code, out, err, path = self.run_enroll(directory, token=token, client_factory=FakeClient)
            store = WorkerCredentialStore(path)
            self.assertEqual(code, 0)
            self.assertFalse(store.pending_path.exists())
            self.assertNotIn(token, snapshots[0])
            self.assertNotIn(token, out + err)
            self.assertIn("SUCCESS: Worker enrolled.", out)
            self.assertIn("Worker ID: worker-clean", out)
            self.assertNotIn("http://control-plane.invalid", out)
            self.assertNotIn("credentials.json", out)
            self.assertEqual(store.load().worker_id, "worker-clean")

    def test_reset_pending_is_explicit_and_does_not_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "credentials.json")
            WorkerCredentialStore(path).save_pending_enrollment("attempt-123456789012", "c" * 32, digest_secret("a" * 32))
            env = {"WORKER_CREDENTIAL_FILE": path}
            from src.worker_agent import cli
            with patch.dict(os.environ, env, clear=True), patch("getpass.getpass") as getpass_prompt, redirect_stdout(io.StringIO()) as output:
                code = cli.main(["enroll", "--reset-pending"])
            self.assertEqual(code, 0)
            self.assertIn("Pending enrollment reset", output.getvalue())
            getpass_prompt.assert_not_called()
            self.assertIsNone(WorkerCredentialStore(path).load_pending_enrollment())

    def test_pending_file_uses_restrictive_posix_permissions(self):
        if os.name == "nt":
            self.skipTest("POSIX mode assertion is not meaningful on Windows")
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "credentials.json")
            store = WorkerCredentialStore(path)
            store.save_pending_enrollment("attempt-123456789012", "c" * 32, digest_secret("a" * 32))
            self.assertEqual(stat.S_IMODE(os.stat(store.pending_path).st_mode), 0o600)

    def test_windows_acl_restriction_uses_icacls_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "credentials.json")
            store = WorkerCredentialStore(path)
            with patch("src.worker_agent.infrastructure.security.credential_store.os", _WindowsOSProxy()), \
                patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run") as run:
                store.save_pending_enrollment("attempt-123456789012", "c" * 32, digest_secret("a" * 32))
            self.assertTrue(run.called)
            args = run.call_args.args[0]
            self.assertEqual(args[0].lower(), "icacls")
            self.assertIn(str(store.pending_path), args)

            with patch("src.worker_agent.infrastructure.security.credential_store.os", _WindowsOSProxy()), \
                patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run", side_effect=OSError("icacls missing")):
                with self.assertRaises(PermissionError):
                    store.save_pending_enrollment("attempt-abcdef123456", "d" * 32, digest_secret("a" * 32))

    def test_windows_acl_failure_removes_empty_temp_before_secret_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "credentials.json")
            store = WorkerCredentialStore(path)
            secret = "d" * 32
            writes = []
            real_dump = json.dump

            def record_dump(payload, stream, *args, **kwargs):
                writes.append(stream.name)
                return real_dump(payload, stream, *args, **kwargs)

            with patch("src.worker_agent.infrastructure.security.credential_store.os", _WindowsOSProxy()), \
                patch("src.worker_agent.infrastructure.security.credential_store.subprocess.run", side_effect=OSError("icacls missing")) as run, \
                patch("src.worker_agent.infrastructure.security.credential_store.json.dump", side_effect=record_dump):
                with self.assertRaises(PermissionError):
                    store.save_pending_enrollment("attempt-abcdef123456", secret, digest_secret("a" * 32))

            temp_path = os.path.join(directory, f".{os.path.basename(store.pending_path)}.tmp")
            run.assert_called_once()
            self.assertEqual(writes, [])
            self.assertFalse(os.path.exists(temp_path))
            for name in os.listdir(directory):
                candidate = os.path.join(directory, name)
                if os.path.isfile(candidate):
                    with open(candidate, encoding="utf-8") as stream:
                        self.assertNotIn(secret, stream.read())


if __name__ == "__main__":
    unittest.main()
