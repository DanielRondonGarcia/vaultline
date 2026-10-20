"""Durable runtime alignment tests.

Every surface that builds, tests, or publishes this project must use the exact
CPython version pinned in ``.python-version`` (currently 3.11.17), and the
public checker ``scripts/check_python_runtime.py`` must prove on that exact
runtime that the source compiles and the Control Plane really starts.
"""

import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
import contextlib
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = ROOT / "scripts" / "check_python_runtime.py"
DOCKERFILE = ROOT / "Dockerfile"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release-dispatch.yml"
INTEGRATION_COMPOSE = ROOT / "test" / "restore-sftpgo-sqlite" / "docker-compose.yml"
WINDOWS_PBS_URL = "cpython-3.11.17%2B20261003-x86_64-pc-windows-msvc-install_only.tar.gz"
WINDOWS_PBS_SHA256 = "0f7defa7a0ed99b61e0df0bba5027474711521f1307cc3830c2f456401beeed5"


def load_checker_module():
    spec = importlib.util.spec_from_file_location("check_python_runtime", CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PythonRuntimePinningTests(unittest.TestCase):
    def setUp(self):
        self.pin = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
        self.dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.ci = CI_WORKFLOW.read_text(encoding="utf-8")
        self.release = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.compose = INTEGRATION_COMPOSE.read_text(encoding="utf-8")

    def test_pin_file_pins_exact_cpython_31117(self):
        # Exact micro version: a moving 3.11.x float would not reproduce the
        # published bug or the checksum-verified interpreter downloads.
        self.assertEqual(self.pin, "3.11.17")
        self.assertRegex(self.pin, r"^\d+\.\d+\.\d+$")

    def test_every_runtime_surface_pins_the_same_exact_version(self):
        # Docker base image
        self.assertIn(f"ARG PYTHON_BASE_IMAGE=python:{self.pin}-slim-bookworm", self.dockerfile)
        # CI reads the common pin instead of a separate floating version
        self.assertIn("python-version-file: .python-version", self.ci)
        self.assertNotRegex(self.ci, r"python-version:\s*[\"']3\.11")
        # Native Linux and Windows interpreters are 3.11.17 standalone builds
        self.assertIn("cpython-3.11.17%2B20261003", self.release)
        self.assertIn(WINDOWS_PBS_URL, self.release)
        # Integration fixtures use the exact Alpine 3.11.17 image
        self.assertEqual(set(re.findall(r"image: python:([\d.]+)-alpine", self.compose)), {self.pin})

    def test_dockerfile_runs_exact_runtime_check_before_any_image_target(self):
        self.assertIn("COPY .python-version /app/.python-version", self.dockerfile)
        self.assertIn("COPY scripts/check_python_runtime.py /app/scripts/check_python_runtime.py", self.dockerfile)
        self.assertIn("RUN python scripts/check_python_runtime.py", self.dockerfile)
        for target in ("backup-runtime", "control-plane", "worker"):
            self.assertLess(
                self.dockerfile.index("RUN python scripts/check_python_runtime.py"),
                self.dockerfile.index(f"FROM app-base AS {target}"),
            )

    def test_ci_runs_the_runtime_checker_after_deps_before_unit_tests(self):
        self.assertLess(
            self.ci.index("python -m pip install -r requirements.txt"),
            self.ci.index("python scripts/check_python_runtime.py"),
        )
        self.assertLess(
            self.ci.index("python scripts/check_python_runtime.py"),
            self.ci.index("python -m unittest discover tests"),
        )

    def windows_job(self):
        match = re.search(r"(?ms)^  native-windows:\n(.*?)(?=^  [\w-]+:\n|\Z)", self.release)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_release_windows_build_uses_checksum_verified_pinned_pbs_interpreter(self):
        job = self.windows_job()
        self.assertNotIn("python-version:", job)
        self.assertIn(WINDOWS_PBS_URL, job)
        self.assertIn(WINDOWS_PBS_SHA256, job)
        # Checksum must be verified before extract/execute, and cmdlet and
        # native failures must both abort the step.
        self.assertIn("$ErrorActionPreference = 'Stop'", job)
        self.assertLess(job.index(WINDOWS_PBS_SHA256), job.index("tar -xzf"))
        self.assertIn("tar -xzf $Archive -C $InstallDir", job)
        self.assertIn('pinned PBS python extract exited with $LASTEXITCODE', job)
        self.assertIn("$env:RUNNER_TEMP", job)
        self.assertIn("Add-Content $env:GITHUB_PATH", job)
        self.assertIn(r'Join-Path $InstallDir "python\Scripts"', job)
        # The explicit version check reads the common .python-version pin and
        # sys.version_info (not shell quoting of python --version).
        self.assertIn("Get-Content -LiteralPath .python-version", job)
        self.assertIn("sys.version_info", job)

    def test_release_native_builds_run_the_checker_before_pyinstaller(self):
        for job_marker in ("native-linux:", "native-windows:"):
            segment = self.release.split(f"  {job_marker}", 1)[1].split("  image-build:", 1)[0]
            self.assertLess(
                segment.index("python scripts/check_python_runtime.py"),
                segment.index("python -m PyInstaller"),
                job_marker,
            )

    def test_checker_isolates_child_state_and_binds_localhost(self):
        checker = load_checker_module()
        self.assertEqual(checker.STARTUP_TIMEOUT_SECONDS, 15)
        self.assertEqual(checker.PROJECT_ROOT, ROOT)
        self.assertEqual(checker.HEALTH_PATH, "/healthz")
        safe_dir = Path("s")
        env = checker.child_env(0, safe_dir)
        self.assertEqual(env["CONTROL_PLANE_REPOSITORY"], "memory")
        self.assertEqual(env["CONTROL_PLANE_HOST"], "127.0.0.1")
        self.assertEqual(env["CONTROL_PLANE_PORT"], "0")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertTrue(checker.ISOLATED_MASTER_KEY)
        self.assertTrue(checker.ISOLATED_SESSION_KEY)
        self.assertEqual(json.loads(checker.ISOLATED_USERS_JSON)[0]["username"], "runtime-check")
        # Every file-backed production state path is redirected into the
        # disposable safe directory, whatever the invoking environment set.
        for name in (
            "CONTROL_PLANE_KEY_FILE",
            "CONTROL_PLANE_SESSION_KEY_FILE",
            "CONTROL_PLANE_USERS_FILE",
            "CONTROL_PLANE_DB_PATH",
            "CONTROL_PLANE_TLS_DIR",
        ):
            self.assertTrue(env[name].startswith(str(safe_dir)), name)


class CheckPythonRuntimeScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state_files = [ROOT / name for name in (
            ".control_plane.key",
            ".control_plane.session.key",
            ".control_plane.users.json",
            "control_plane.db",
        )]
        cls.digests_before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in cls.state_files if path.exists()}

    def test_script_reports_success_on_the_pinned_runtime(self):
        result = subprocess.run(
            [sys.executable, str(CHECKER_PATH)], capture_output=True, text=True, timeout=90, cwd=str(ROOT)
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertIn("python runtime matches .python-version: CPython 3.11.17", result.stdout)
        self.assertIn('control plane startup check passed: GET /healthz returned HTTP 200 {"ok": true}', result.stdout)
        # The isolated memory-mode child must not write key, session, user or
        # database state into any working tree.
        digests_after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in self.state_files if path.exists()}
        self.assertEqual(digests_after, self.digests_before)

    def test_wrong_runtime_fails_with_exact_expected_and_actual_versions(self):
        # Public CLI path: a wrapper interpreter reports a wrong version and
        # the real script must respond with the exact evidence and exit 2.
        wrapper = (
            "import sys; sys.version_info = (3, 11, 15, 'final', 0); "
            f"exec(compile(open(r'{CHECKER_PATH}', encoding='utf-8').read(), r'{CHECKER_PATH}', 'exec'), "
            "{'__name__': '__main__', '__file__': r'%s'})" % CHECKER_PATH
        )
        result = subprocess.run([sys.executable, "-c", wrapper], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("pins CPython 3.11.17", result.stderr)
        self.assertIn("running on 3.11.15", result.stderr)

    def test_internal_enforce_runtime_keeps_exit_code_two(self):
        checker = load_checker_module()
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(stderr):
            checker.enforce_runtime((3, 11, 15, "final", 0), "3.11.17")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("pins CPython 3.11.17", stderr.getvalue())
        self.assertIn("running on 3.11.15", stderr.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            checker.enforce_runtime((3, 11, 17, "final", 0), "3.11.17")

    def test_syntax_guard_reports_exact_file_line_and_message(self):
        checker = load_checker_module()
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad_fstring.py"
            bad.write_bytes(b'value = f"{a if a else \\"\\\\u2014\\"}"\n')
            original_root = checker.SOURCE_ROOT
            checker.SOURCE_ROOT = Path(directory)
            try:
                with self.assertRaises(SystemExit) as caught:
                    checker.compile_sources()
            finally:
                checker.SOURCE_ROOT = original_root
            message = caught.exception.code
            self.assertIn("bad_fstring.py:1:", message)
            self.assertIn("f-string expression part cannot include a backslash", message)
    def test_startup_deadline_reaps_a_hung_child_bounded(self):
        """A child that never binds the port must fail the deadline, bounded,
        with no orphan process left behind."""
        checker = load_checker_module()
        captured = {}
        original_start = checker.start_child
        original_timeout = checker.STARTUP_TIMEOUT_SECONDS

        def hanging_child(port, safe_dir):
            captured["proc"] = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return captured["proc"]

        checker.STARTUP_TIMEOUT_SECONDS = 3
        checker.start_child = hanging_child
        started = time.monotonic()
        try:
            with self.assertRaises(SystemExit) as caught:
                checker.startup_check()
        finally:
            checker.STARTUP_TIMEOUT_SECONDS = original_timeout
            checker.start_child = original_start
        self.assertLess(time.monotonic() - started, 15)
        self.assertIn("no /healthz HTTP 200 within 3s", str(caught.exception.code))
        # Reaped, not orphaned.
        self.assertIsNotNone(captured["proc"].poll())

    def test_wrong_health_body_fails_immediately_and_reaps_the_child(self):
        """HTTP 200 with a non-{ok: true} body must fail with the exact body
        evidence, never label a missing value as success, and leave no orphan."""
        checker = load_checker_module()
        fixture_code = """
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class WrongHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"version": "wrong-body"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), WrongHealthHandler).serve_forever()
"""
        captured = {}
        original_start = checker.start_child
        original_timeout = checker.STARTUP_TIMEOUT_SECONDS

        def wrong_body_child(port, safe_dir):
            captured["proc"] = subprocess.Popen(
                [sys.executable, "-c", fixture_code, str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return captured["proc"]

        checker.STARTUP_TIMEOUT_SECONDS = 10
        checker.start_child = wrong_body_child
        try:
            with self.assertRaises(SystemExit) as caught:
                checker.startup_check()
        finally:
            checker.STARTUP_TIMEOUT_SECONDS = original_timeout
            checker.start_child = original_start
        message = str(caught.exception.code)
        self.assertIn("unexpected body", message)
        self.assertIn("wrong-body", message)
        self.assertIsNotNone(captured["proc"].poll())


if __name__ == "__main__":
    unittest.main()