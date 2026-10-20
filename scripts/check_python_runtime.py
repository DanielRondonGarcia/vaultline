"""Public Control Plane runtime checker.

Run with the project interpreter only:  python scripts/check_python_runtime.py

Guarantees, before any build, test run or image is published:
1. The interpreter matches the exact version pinned in ``.python-version``
   (currently CPython 3.11.17), not merely the same minor release.
2. Every project source file under ``src`` compiles on that interpreter
   (in memory only, no ``__pycache__`` is written).
3. The real Control Plane process starts with isolated in-memory state
   (file-path based state overridden into a system temp directory) and serves
   ``GET /healthz`` with HTTP 200 and exactly ``{"ok": true}`` on an ephemeral
   127.0.0.1 port.

The checker is stdlib only and takes no arguments.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PIN_FILE = PROJECT_ROOT / ".python-version"
SOURCE_ROOT = PROJECT_ROOT / "src"
STARTUP_TIMEOUT_SECONDS = 15
HEALTH_PATH = "/healthz"
EXPECTED_HEALTH_TIMEOUT = 2
REAP_TIMEOUT_SECONDS = 2

# Deterministic isolated secrets/state so the child never reads or writes
# .control_plane.key/.control_plane.session.key/.control_plane.users.json/
# control_plane.db in any working tree, whatever the invoking environment sets.
ISOLATED_MASTER_KEY = "runtime-check-master-key-0123456789abcdef01234567"
ISOLATED_SESSION_KEY = "runtime-check-session-key-0123456789abcdef01234567"
ISOLATED_USERS_JSON = json.dumps(
    [
        {
            "username": "runtime-check",
            "password": "unused",
            "role": "admin",
            "must_change_password": False,
            "password_scheme": "plain",
        }
    ]
)


def pin_version() -> str:
    """Read the exact version pin from .python-version, normalized."""
    return PIN_FILE.read_text(encoding="utf-8").strip()


def enforce_runtime(actual: tuple, expected: str) -> None:
    """Exit non-zero when the running interpreter does not match the pin."""
    actual_text = ".".join(str(part) for part in actual[:3])
    if actual_text == expected:
        return
    sys.stderr.write(
        "python runtime mismatch: .python-version pins CPython {expected}, "
        "but this checker is running on {actual}\n".format(expected=expected, actual=actual_text)
    )
    raise SystemExit(2)


def compile_sources() -> int:
    """Compile every project .py under src in memory; no bytecode on disk."""
    source_paths = sorted(path for path in SOURCE_ROOT.rglob("*.py") if "__pycache__" not in path.parts)
    for path in source_paths:
        source = path.read_bytes()
        try:
            compile(source, str(path), "exec")
        except SyntaxError as error:
            text = error.text
            line = text.decode("utf-8", "replace") if isinstance(text, bytes) else (text or "")
            raise SystemExit(
                "syntax error on CPython {version}: {path}:{line_number}:{offset}: {message}\n{line}".format(
                    version=".".join(str(part) for part in sys.version_info[:3]),
                    path=path,
                    line_number=error.lineno,
                    offset=error.offset,
                    message=error.msg,
                    line=line.rstrip("\n") if line else "",
                )
            ) from None
    return len(source_paths)


def ephemeral_localhost_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def child_env(port: int, safe_dir: Path) -> dict:
    """Child environment: forced runtime overrides plus safe state paths."""
    return dict(
        os.environ,
        PYTHONDONTWRITEBYTECODE="1",
        CONTROL_PLANE_REPOSITORY="memory",
        CONTROL_PLANE_MASTER_KEY=ISOLATED_MASTER_KEY,
        CONTROL_PLANE_SESSION_KEY=ISOLATED_SESSION_KEY,
        CONTROL_PLANE_USERS_JSON=ISOLATED_USERS_JSON,
        CONTROL_PLANE_KEY_FILE=str(safe_dir / "isolated.key"),
        CONTROL_PLANE_SESSION_KEY_FILE=str(safe_dir / "isolated.session.key"),
        CONTROL_PLANE_USERS_FILE=str(safe_dir / "isolated.users.json"),
        CONTROL_PLANE_DB_PATH=str(safe_dir / "isolated.db"),
        CONTROL_PLANE_TLS_DIR=str(safe_dir / "isolated.tls"),
        CONTROL_PLANE_HOST="127.0.0.1",
        CONTROL_PLANE_PORT=str(port),
        CONTROL_PLANE_TLS_ENABLED="0",
        APP_VERSION="runtime-check",
    )


def start_child(port: int, safe_dir: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "src.control_plane.main"],
        cwd=str(PROJECT_ROOT),
        env=child_env(port, safe_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def read_child_output(proc: subprocess.Popen) -> tuple[str, str]:
    """Bounded drain of the child output after fully stopping it (no orphan)."""
    stdout, stderr = "", ""
    try:
        stdout, stderr = proc.communicate(timeout=REAP_TIMEOUT_SECONDS)
        return stdout or "", stderr or ""
    except subprocess.TimeoutExpired:
        pass
    # The child is still alive after the bounded wait: terminate it BEFORE
    # draining further, so a hung child can never block the checker forever.
    proc.terminate()
    try:
        stdout, stderr = proc.communicate(timeout=REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # Both bounded attempts expired; give up on any further output.
            return "", ""
    return stdout or "", stderr or ""


def startup_check() -> None:
    """Launch the real Control Plane and wait for a healthy /healthz response."""
    port = ephemeral_localhost_port()
    # Bypass proxies for loopback: a host proxy must not intercept the
    # isolated health request.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix="control-plane-runtime-check-") as temporary_root:
        proc = start_child(port, Path(temporary_root))
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        try:
            while True:
                if proc.poll() is not None:
                    stdout, stderr = read_child_output(proc)
                    raise SystemExit(
                        "control plane startup check failed: exited with code {code} before being healthy\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}".format(
                            code=proc.returncode, stdout=stdout, stderr=stderr
                        )
                    )
                if time.monotonic() >= deadline:
                    stdout, stderr = read_child_output(proc)
                    raise SystemExit(
                        "control plane startup check failed: no /healthz HTTP 200 within {seconds}s\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}".format(
                            seconds=STARTUP_TIMEOUT_SECONDS, stdout=stdout, stderr=stderr
                        )
                    )
                url = f"http://127.0.0.1:{port}{HEALTH_PATH}"
                try:
                    with opener.open(url, timeout=EXPECTED_HEALTH_TIMEOUT) as response:
                        status = response.status
                        body = response.read().decode("utf-8")
                except urllib.error.HTTPError as error:
                    # Non-200 answers are judged immediately as wrong health.
                    status = error.code
                    try:
                        body = error.read().decode("utf-8")
                    except OSError:
                        body = ""
                except (urllib.error.URLError, ConnectionError, OSError):
                    # The server is not accepting connections yet: keep polling.
                    time.sleep(0.25)
                    continue
                # An HTTP answer arrived: judge it immediately.
                try:
                    payload = json.loads(body)
                except ValueError:
                    payload = None
                if status == 200 and payload == {"ok": True}:
                    print('control plane startup check passed: GET /healthz returned HTTP 200 {"ok": true}')
                    return
                stdout, stderr = read_child_output(proc)
                raise SystemExit(
                    'control plane startup check failed: GET /healthz answered HTTP {status} with unexpected body {body}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}'.format(
                        status=status, body=repr(body), stdout=stdout, stderr=stderr
                    )
                )
        finally:
            # Reap the child unconditionally: terminate first, then kill, so
            # no process outlives the checker on success or failure.
            read_child_output(proc)


def main() -> None:
    expected = pin_version()
    enforce_runtime(sys.version_info, expected)
    print("python runtime matches .python-version: CPython {version}".format(version=expected))
    compiled = compile_sources()
    print("compiled {count} project source files under src without syntax errors".format(count=compiled))
    startup_check()


if __name__ == "__main__":
    main()