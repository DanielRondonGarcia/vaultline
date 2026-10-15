import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.control_plane.infrastructure.security.worker_auth import WorkerAuthState
from src.control_plane.main import ControlPlaneApplication, ControlPlaneHTTPServer, ControlPlaneRequestHandler
from src.security.hmac_protocol import digest_secret, sign_request
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient
from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore


class WorkerEnrollmentV2StateTests(unittest.TestCase):
    def auth_states(self):
        yield "memory", WorkerAuthState()
        with tempfile.TemporaryDirectory() as directory:
            yield "sqlite", WorkerAuthState(os.path.join(directory, "control-plane.db"))

    def test_v2_completion_uses_client_credential_and_replays_same_attempt(self):
        for name, auth in self.auth_states():
            with self.subTest(name=name):
                bootstrap = "b" * 32
                credential = "c" * 32
                auth.create_enrollment("worker", "host", {"role": "native"}, bootstrap, worker_id="worker-v2")

                first = auth.complete_v2(bootstrap, "attempt-1234567890", credential, labels={"zone": "a"})
                replay = auth.complete_v2(bootstrap, "attempt-1234567890", credential, labels={"zone": "b"})

                self.assertEqual(first["worker_id"], "worker-v2")
                self.assertEqual(replay["worker_id"], first["worker_id"])
                self.assertEqual(replay["credential_version"], first["credential_version"])
                self.assertEqual(first["labels"], {"zone": "a"})
                self.assertEqual(replay["labels"], first["labels"])
                stored = auth._get("worker-v2", first["credential_version"])
                self.assertEqual(stored.secret_digest, digest_secret(credential))
                self.assertNotEqual(stored.secret_digest, digest_secret(bootstrap))

    def test_v2_used_enrollment_rejects_different_attempt_or_credential(self):
        for name, auth in self.auth_states():
            with self.subTest(name=name):
                bootstrap = "d" * 32
                credential = "e" * 32
                auth.create_enrollment("worker", "host", {}, bootstrap, worker_id="worker-v2-conflict")
                auth.complete_v2(bootstrap, "attempt-abcdef123456", credential)

                with self.assertRaises(ValueError):
                    auth.complete_v2(bootstrap, "attempt-other123456", credential)
                with self.assertRaises(ValueError):
                    auth.complete_v2(bootstrap, "attempt-abcdef123456", "f" * 32)

    def test_v2_safe_replay_survives_expiry_after_commit(self):
        auth = WorkerAuthState()
        bootstrap = "g" * 32
        credential = "h" * 32
        auth.create_enrollment("worker", "host", {}, bootstrap, worker_id="worker-v2-expiry")
        first = auth.complete_v2(bootstrap, "attempt-expiry123456", credential)
        enrollment = auth._enrollment(digest_secret(bootstrap))
        enrollment.expires_at = enrollment.expires_at.replace(year=2000)

        replay = auth.complete_v2(bootstrap, "attempt-expiry123456", credential)

        self.assertEqual(replay["credential_version"], first["credential_version"])

    def test_v2_rejections_carry_distinct_codes_for_unknown_used_and_expired_tokens(self):
        from src.control_plane.infrastructure.security.worker_auth import EnrollmentRejectedError

        for name, auth in self.auth_states():
            with self.subTest(name=name):
                credential = "n" * 32
                with self.assertRaises(EnrollmentRejectedError) as unknown:
                    auth.complete_v2("u" * 32, "attempt-unknown12345", credential)
                self.assertEqual(unknown.exception.code, "enrollment_unknown")
                self.assertEqual(str(unknown.exception), "unknown worker enrollment token")

                used_token = "v" * 32
                auth.create_enrollment("worker", "host", {}, used_token, worker_id="worker-used")
                auth.complete_v2(used_token, "attempt-used12345678", credential)
                # A different attempt against a V2-consumed token hits the replay guard, not the code path.
                with self.assertRaises(ValueError) as replay_conflict:
                    auth.complete_v2(used_token, "attempt-other1234567", credential)
                self.assertEqual(str(replay_conflict.exception), "invalid worker enrollment request")

                expired_token = "w" * 32
                auth.create_enrollment("worker", "host", {}, expired_token, worker_id="worker-expired", ttl_minutes=1)
                self._expire(auth, expired_token)
                with self.assertRaises(EnrollmentRejectedError) as expired:
                    auth.complete_v2(expired_token, "attempt-expired12345", credential)
                self.assertEqual(expired.exception.code, "enrollment_expired")

                reused_token = "x" * 32
                auth.create_enrollment("worker", "host", {}, reused_token, worker_id="worker-reused")
                self._mark_used_without_attempt(auth, reused_token)
                with self.assertRaises(EnrollmentRejectedError) as used:
                    auth.complete_v2(reused_token, "attempt-reused123456", credential)
                self.assertEqual(used.exception.code, "enrollment_used")

    @staticmethod
    def _expire(auth, token):
        digest = digest_secret(token)
        if auth.path:
            with auth._db() as db:
                db.execute("UPDATE auth_enrollments SET expires_at=? WHERE secret_digest=?", ("2000-01-01T00:00:00", digest))
        else:
            enrollment = auth._enrollments[digest]
            enrollment.expires_at = enrollment.expires_at.replace(year=2000)

    @staticmethod
    def _mark_used_without_attempt(auth, token):
        # Simulates a V1 completion (or an older server) that consumed the token
        # without recording a V2 attempt, so the V2 path sees used=1 directly.
        auth._mark_used(digest_secret(token))

    def test_v2_rejects_short_credential_and_attempt_id(self):
        auth = WorkerAuthState()
        bootstrap = "i" * 32
        auth.create_enrollment("worker", "host", {}, bootstrap, worker_id="worker-v2-validation")

        for attempt_id, credential in (("short", "j" * 32), ("attempt-valid123456", "k" * 31)):
            with self.subTest(attempt_id=attempt_id, credential_length=len(credential)):
                with self.assertRaises(ValueError):
                    auth.complete_v2(bootstrap, attempt_id, credential)


class WorkerEnrollmentV2ApiTests(unittest.TestCase):
    def setUp(self):
        self.auth = WorkerAuthState()
        self.registered = []

        class Service:
            def __init__(inner_self, outer):
                inner_self.worker_auth = outer.auth
                inner_self.outer = outer

            def register_worker(inner_self, name, host_name, labels, worker_id):
                inner_self.outer.registered.append((name, host_name, labels, worker_id))
                return SimpleNamespace(id=worker_id)

        application = ControlPlaneApplication(auth_service=None, control_plane_service=Service(self))
        self.server = ControlPlaneHTTPServer(("127.0.0.1", 0), ControlPlaneRequestHandler, application=application)
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        import threading

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_v2_endpoint_does_not_return_secret_and_enables_hmac_with_client_credential(self):
        bootstrap = "l" * 32
        credential = "m" * 32
        self.auth.create_enrollment("native", "host", {"os": "linux"}, bootstrap, worker_id="worker-api")
        client = ControlPlaneClient(self.base_url)

        response = client._post(
            "/api/v2/worker-enrollments/complete",
            {"secret": bootstrap, "attempt_id": "attempt-api123456", "credential_secret": credential, "labels": {"os": "linux"}},
            authenticate=False,
        )

        self.assertEqual(response, {"worker_id": "worker-api", "credential_version": "1"})
        self.assertEqual(self.registered[-1], ("native", "host", {"os": "linux"}, "worker-api"))
        body = b"{}"
        timestamp = "1700000000"
        signature = sign_request(digest_secret(credential), "POST", "/api/v1/workers/worker-api/heartbeat", body, timestamp, "nonce", "worker-api", "1")
        with patch("time.time", return_value=int(timestamp)):
            self.auth.authenticate("worker-api", "POST", "/api/v1/workers/worker-api/heartbeat", body, timestamp, "nonce", "worker-api", "1", signature)

    def test_v2_endpoint_returns_400_with_error_and_code_that_the_client_surfaces(self):
        from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneHTTPError

        client = ControlPlaneClient(self.base_url)
        expired = "r" * 32
        self.auth.create_enrollment("native", "host", {}, expired, worker_id="worker-api-expired")
        self.auth._enrollments[digest_secret(expired)].expires_at = self.auth._enrollments[digest_secret(expired)].expires_at.replace(year=2000)

        for token, expected_code, expected_error in (
            ("s" * 32, "enrollment_unknown", "unknown worker enrollment token"),
            (expired, "enrollment_expired", "worker enrollment token expired"),
        ):
            with self.subTest(code=expected_code):
                with self.assertRaises(ControlPlaneHTTPError) as raised:
                    client.complete_worker_enrollment_v2(token, "attempt-api-reject123", "t" * 32)
                error = raised.exception
                self.assertEqual(error.status, 400)
                self.assertEqual(error.server_code, expected_code)
                self.assertEqual(error.detail, expected_error)
                self.assertEqual(
                    str(error),
                    f"Control Plane rejected POST /api/v2/worker-enrollments/complete: HTTP 400 (code={expected_code}): {expected_error}",
                )
        self.assertEqual(self.registered, [])

    def test_v2_endpoint_replay_ignores_changed_labels_for_registration_metadata(self):
        bootstrap = "p" * 32
        credential = "q" * 32
        self.auth.create_enrollment("native", "host", {"os": "linux"}, bootstrap, worker_id="worker-api-replay")
        client = ControlPlaneClient(self.base_url)
        payload = {"secret": bootstrap, "attempt_id": "attempt-replay123456", "credential_secret": credential}

        first = client._post(
            "/api/v2/worker-enrollments/complete",
            {**payload, "labels": {"os": "linux", "zone": "a"}},
            authenticate=False,
        )
        replay = client._post(
            "/api/v2/worker-enrollments/complete",
            {**payload, "labels": {"os": "linux", "zone": "b"}},
            authenticate=False,
        )

        self.assertEqual(replay, first)
        self.assertEqual(self.registered[-2:], [
            ("native", "host", {"os": "linux", "zone": "a"}, "worker-api-replay"),
            ("native", "host", {"os": "linux", "zone": "a"}, "worker-api-replay"),
        ])


class WorkerEnrollmentV2ClientTests(unittest.TestCase):
    def test_client_uses_supplied_stable_credential_and_attempt_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkerCredentialStore(os.path.join(directory, "credential.json"))
            client = ControlPlaneClient("http://control-plane", credential_store=store)
            supplied_credential = "r" * 32
            supplied_attempt = "attempt-client123456"
            calls = []

            def capture_request(path, payload, authenticate=True):
                calls.append((path, dict(payload)))
                if len(calls) == 1:
                    raise RuntimeError("network failed")
                return {"worker_id": "worker-client", "credential_version": "7"}

            with patch.object(client, "_post", side_effect=capture_request):
                with self.assertRaises(RuntimeError):
                    client.complete_worker_enrollment_v2(
                        "n" * 32,
                        supplied_attempt,
                        supplied_credential,
                        labels={"os": "linux"},
                    )
                self.assertIsNone(store.load())
                result = client.complete_worker_enrollment_v2(
                    "n" * 32,
                    supplied_attempt,
                    supplied_credential,
                    labels={"os": "linux"},
                )

            self.assertEqual(calls[0], calls[1])
            self.assertEqual(calls[0][0], "/api/v2/worker-enrollments/complete")
            self.assertEqual(calls[0][1], {
                "secret": "n" * 32,
                "attempt_id": supplied_attempt,
                "credential_secret": supplied_credential,
                "labels": {"os": "linux"},
            })
            self.assertNotIn("credential_secret", {k: v for k, v in result.items() if k != "durable_credential_secret"})
            self.assertEqual(result["durable_credential_secret"], supplied_credential)
            self.assertEqual(store.load().secret, supplied_credential)
            self.assertEqual(client.worker_id, "worker-client")

    def test_client_requires_caller_supplied_credential_and_attempt(self):
        client = ControlPlaneClient("http://control-plane")

        with self.assertRaises(TypeError):
            client.complete_worker_enrollment_v2("o" * 32, labels={})


if __name__ == "__main__":
    unittest.main()
