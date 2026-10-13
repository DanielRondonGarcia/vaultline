"""Small stdlib-only CLI wrapper for native worker operations."""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Sequence

from src.app import main as app_main
from src.security.hmac_protocol import digest_secret
from src.worker_agent import main as worker_main
from src.worker_agent._build_info import VERSION as BUILD_VERSION
from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient
from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore
from src.worker_agent.state_paths import (
    resolve_credential_file,
    resolve_enrollment_config_file,
    resolve_state_dir,
    secure_state_dir,
    state_dir_is_managed,
)


def _env_present(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def _file_exists_from_env(name: str, default: str | None = None) -> bool:
    raw = os.environ.get(name)
    value = raw if raw is not None else default
    if not value or not value.strip():
        return False
    return Path(value).exists()


def _configuration_presence() -> dict[str, bool]:
    """Return redacted configuration presence without exposing values or paths."""

    return {
        "control_plane_url_configured": _env_present("CONTROL_PLANE_URL"),
        "worker_name_configured": _env_present("WORKER_NAME"),
        "worker_id_configured": _env_present("WORKER_ID"),
        "enrollment_token_configured": _env_present("WORKER_ENROLLMENT_TOKEN")
        or _env_present("WORKER_SECRET"),
        "state_dir_configured": _env_present("WORKER_STATE_DIR"),
        "enrollment_config_exists": resolve_enrollment_config_file().exists(),
        "credential_file_configured": _env_present("WORKER_CREDENTIAL_FILE"),
        "credential_file_exists": resolve_credential_file().exists(),
        "ca_file_configured": _env_present("CONTROL_PLANE_CA_FILE"),
        "ca_file_exists": _file_exists_from_env("CONTROL_PLANE_CA_FILE"),
    }


def _self_check(_args: argparse.Namespace) -> int:
    payload = {
        "configuration": _configuration_presence(),
        "runtime": NativeRuntimeAdapter().self_check(),
        "notes": [
            "Executable availability is reported for diagnostics; feature-dependent tools must be installed manually.",
            "Configuration values, URLs, tokens, credentials, signatures, and stored credential contents are intentionally redacted.",
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _daemon(args: argparse.Namespace) -> int:
    os.environ["WORKER_RUNTIME"] = "native"
    os.environ["WORKER_RUN_ONCE"] = "true" if args.once else "false"
    try:
        worker_main.main()
    except RuntimeError as exc:
        if str(exc) == "worker is not enrolled":
            print("error: worker is not enrolled; run 'vaultline-worker enroll --control-plane-url <url>'", file=sys.stderr)
            return 3
        raise
    return 0


def _backup_engine(_args: argparse.Namespace) -> int:
    try:
        app_main.main()
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    return 0


def _enrollment_labels() -> dict[str, str]:
    return {
        "runtime_kind": "native",
        "runtime_type": "native",
        "supported_runtimes": "native",
        "host_name": socket.gethostname(),
    }


def _new_pending_enrollment(store: WorkerCredentialStore, token_digest: str):
    attempt_id = f"attempt-{uuid.uuid4()}"
    durable_credential = secrets.token_urlsafe(48)
    return store.save_pending_enrollment(attempt_id, durable_credential, token_digest)


def _optional(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None


def _enrollment_config_payload(control_plane_url: str, ca_file: str | None, worker_name: str | None) -> dict[str, str]:
    payload = {"control_plane_url": control_plane_url}
    if ca_file:
        payload["control_plane_ca_file"] = ca_file
    if worker_name:
        payload["worker_name"] = worker_name
    return payload


def _enroll(args: argparse.Namespace) -> int:
    store = WorkerCredentialStore(resolve_credential_file())
    if args.reset_pending:
        store.delete_pending_enrollment()
        print("Pending enrollment reset. Run enroll again with a valid bootstrap token to start over.")
        return 0

    control_plane_url = _optional(args.control_plane_url) or _optional(os.environ.get("CONTROL_PLANE_URL"))
    if not control_plane_url:
        print(
            "Control Plane URL is required: pass --control-plane-url <url> or set CONTROL_PLANE_URL.",
            file=sys.stderr,
        )
        return 2
    ca_file = _optional(args.ca_file) or _optional(os.environ.get("CONTROL_PLANE_CA_FILE"))
    worker_name = _optional(args.worker_name) or _optional(os.environ.get("WORKER_NAME"))

    # enroll runs elevated (administrator/root): lock the dedicated state
    # directory down before any enrollment state lands in it, so the service
    # never adopts files a standard user could have planted there.
    if state_dir_is_managed() and _is_windows() and not is_user_admin():
        print("error: open PowerShell as Administrator and rerun 'vaultline-worker enroll ...'", file=sys.stderr)
        return 1

    if state_dir_is_managed():
        state_dir = resolve_state_dir()
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            secure_state_dir(state_dir)
        except OSError as exc:
            print(
                "error: could not secure worker state; run 'vaultline-worker enroll ...' from PowerShell as Administrator or as root.",
                file=sys.stderr,
            )
            return 1

    bootstrap_token = getpass.getpass("Bootstrap token: ")
    token_digest = digest_secret(bootstrap_token)
    pending = store.load_pending_enrollment()
    if pending is None:
        pending = _new_pending_enrollment(store, token_digest)
    elif pending.token_digest != token_digest:
        print(
            "A pending enrollment already exists for a different bootstrap token. "
            "Re-run with the original token, or run 'enroll --reset-pending' if you intentionally want to discard the pending attempt.",
            file=sys.stderr,
        )
        return 2

    client = ControlPlaneClient(
        control_plane_url,
        ca_file=ca_file,
        credential_store=store,
    )
    try:
        response = client.complete_worker_enrollment_v2(
            bootstrap_token,
            pending.attempt_id,
            pending.durable_credential,
            labels=_enrollment_labels(),
        )
    except Exception as exc:
        print(
            "Enrollment did not complete. The pending attempt was kept; re-run enroll with the same bootstrap token to retry.",
            file=sys.stderr,
        )
        print(f"Enrollment error type: {exc.__class__.__name__}", file=sys.stderr)
        return 1

    if store.load() is None:
        store.save(response["worker_id"], pending.durable_credential, response["credential_version"])
    store.delete_pending_enrollment()
    # Persist only after success so a failed attempt never leaves a half-configured daemon.
    store.write_json(
        resolve_enrollment_config_file(),
        _enrollment_config_payload(control_plane_url, ca_file, worker_name),
    )
    print(
        "Enrollment completed. Durable worker credentials and the Control Plane connection settings "
        "were stored for daemon use."
    )
    return 0


def is_user_admin() -> bool:
    """Return whether this Windows process has an elevated administrator token."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


SERVICE_VERBS = ("install", "uninstall", "start", "stop", "status", "run")
LINUX_SERVICE_UNIT = "docker-volume-backup-worker.service"
_LINUX_PACKAGE_MANAGED_VERBS = frozenset({"install", "uninstall", "run"})


def _is_windows() -> bool:
    return sys.platform == "win32"


def _windows_service_error_message(verb: str, error: Exception) -> str:
    from src.worker_agent.windows_service import (
        ERROR_FAILED_SERVICE_CONTROLLER_CONNECT,
        SERVICE_NAME,
    )

    winerror = getattr(error, "winerror", None)
    if verb == "run" and winerror == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
        return (
            "'service run' is the Service Control Manager entry point and cannot be started from an "
            "interactive console; use 'daemon' to run the worker in the foreground."
        )
    if getattr(error, "access_denied", False):
        return f"Access denied while running 'service {verb}': run from an elevated PowerShell (Run as administrator)."
    if getattr(error, "not_installed", False):
        return f"Service {SERVICE_NAME} is not installed; run 'service install' from an elevated PowerShell."
    return f"service {verb} failed: {error}"


def _service_windows(verb: str) -> int:
    from src.worker_agent import windows_service

    try:
        if verb == "run":
            windows_service.run_as_service()
            return 0
        if verb == "install":
            result = windows_service.install_service()
            print(f"Service {result['name']} {result['action']}: {result['command_line']}")
            return 0
        if verb == "uninstall":
            result = windows_service.uninstall_service()
            print(f"Service {result['name']} {result['action']}")
            return 0
        actions = {
            "start": windows_service.start_service,
            "stop": windows_service.stop_service,
            "status": windows_service.service_status,
        }
        print(json.dumps(actions[verb](), sort_keys=True))
        return 0
    except ImportError as exc:
        print(f"pywin32 is required to manage the Windows service (pip install pywin32): {exc}", file=sys.stderr)
        return 1
    except windows_service.ServiceError as exc:
        print(_windows_service_error_message(verb, exc), file=sys.stderr)
        return 1


def _service_systemd(verb: str) -> int:
    if verb in _LINUX_PACKAGE_MANAGED_VERBS:
        print(
            f"'service {verb}' is not available on this platform: the Debian package installs and manages "
            f"{LINUX_SERVICE_UNIT}; use 'daemon' to run the worker in the foreground.",
            file=sys.stderr,
        )
        return 2
    systemctl = shutil.which("systemctl")
    if not systemctl:
        print("systemctl was not found on PATH; this host does not appear to run systemd.", file=sys.stderr)
        return 1
    if verb == "status":
        # is-active/is-enabled exit non-zero for inactive/disabled units; the
        # state is reported as data rather than as a command failure.
        payload = {"name": LINUX_SERVICE_UNIT}
        for key, query in (("active", "is-active"), ("enabled", "is-enabled")):
            completed = subprocess.run([systemctl, query, LINUX_SERVICE_UNIT], capture_output=True, text=True)
            payload[key] = (completed.stdout or "").strip() or "unknown"
        print(json.dumps(payload, sort_keys=True))
        return 0
    completed = subprocess.run([systemctl, verb, LINUX_SERVICE_UNIT])
    return completed.returncode


def _service(args: argparse.Namespace) -> int:
    if _is_windows():
        return _service_windows(args.verb)
    return _service_systemd(args.verb)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vaultline-worker",
        description="Native filesystem worker CLI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {BUILD_VERSION}")
    subcommands = parser.add_subparsers(dest="command")

    enroll = subcommands.add_parser(
        "enroll",
        help="Securely complete native worker first-run enrollment using a hidden bootstrap token prompt.",
    )
    enroll.add_argument(
        "--control-plane-url",
        default=None,
        help="Control Plane base URL (required unless CONTROL_PLANE_URL is set). Persisted to worker.json for the daemon.",
    )
    enroll.add_argument(
        "--ca-file",
        default=None,
        help="Optional CA bundle used to verify the Control Plane TLS certificate (falls back to CONTROL_PLANE_CA_FILE).",
    )
    enroll.add_argument(
        "--worker-name",
        default=None,
        help="Optional worker display name persisted for the daemon (falls back to WORKER_NAME).",
    )
    enroll.add_argument(
        "--reset-pending",
        action="store_true",
        help="Discard a pending native enrollment attempt without prompting for a bootstrap token.",
    )
    enroll.set_defaults(handler=_enroll)

    daemon = subcommands.add_parser(
        "daemon",
        help="Run the native worker in the foreground with continuous polling by default.",
    )
    daemon.add_argument(
        "--once",
        action="store_true",
        help="Debug mode: run a single worker polling cycle instead of the foreground daemon loop.",
    )
    daemon.set_defaults(handler=_daemon)

    self_check = subcommands.add_parser(
        "self-check",
        help="Print a redacted native worker diagnostic payload.",
    )
    self_check.set_defaults(handler=_self_check)

    service = subcommands.add_parser(
        "service",
        help=(
            "Manage the OS service hosting the worker: Windows SCM service VaultlineWorker "
            f"(install/uninstall need an elevated PowerShell), or systemd unit {LINUX_SERVICE_UNIT} on Linux."
        ),
    )
    service.add_argument(
        "verb",
        choices=SERVICE_VERBS,
        help="install|uninstall|run are Windows-only (Linux packages manage the unit); 'run' is the SCM entry point.",
    )
    service.set_defaults(handler=_service)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["backup-engine"]:
        return _backup_engine(argparse.Namespace(command="backup-engine"))
    parser = build_parser()
    if not arguments:
        parser.print_help()
        return 0
    args = parser.parse_args(arguments)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        return args.handler(args)
    except OSError as exc:
        if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5 or getattr(exc, "errno", None) in (1, 13):
            print("error: worker state could not be saved; rerun from an elevated PowerShell (Run as administrator) or as root.", file=sys.stderr)
        else:
            print("error: worker state could not be saved; check filesystem permissions and retry.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
