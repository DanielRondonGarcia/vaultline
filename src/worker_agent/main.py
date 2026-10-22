import json
import logging
import math
import os
import re
import socket
import ssl
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError

from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent._build_info import VERSION as BUILD_VERSION
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
from src.worker_agent.infrastructure.adapters.kubernetes_runtime import KubernetesRuntimeAdapter
from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter
from src.worker_agent.infrastructure.adapters.redis_cache import RedisSnapshotCache
from src.worker_agent.infrastructure.api_client.control_plane_client import (
    ControlPlaneClient,
    ControlPlaneHTTPError,
)
from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore
from src.worker_agent.state_paths import (
    enrollment_files_trusted,
    load_enrollment_config,
    resolve_credential_file,
    resolve_enrollment_config_file,
    state_dir_is_managed,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Process-wide stop request honored by the main loop sleeps so a service host
# (Windows SCM, systemd) can stop the worker cleanly between loop iterations.
_STOP_EVENT = Event()
_WAITING_FOR_ENROLLMENT = "waiting for enrollment"
_IDLE_LOG_EVERY_CYCLES = 20
# State directories whose enrollment files were already refused; the error is
# logged once per directory so an idle service does not flood its log.
_UNTRUSTED_ENROLLMENT_DIRS: set[str] = set()


def request_stop() -> None:
    """Ask the main loop to exit after the current iteration."""
    _STOP_EVENT.set()


def reset_stop() -> None:
    """Clear a previous stop request before hosting a new main loop."""
    _STOP_EVENT.clear()


def stop_requested() -> bool:
    return _STOP_EVENT.is_set()


def _wait_for_stop(delay: float) -> bool:
    """Sleep up to ``delay`` seconds; return True as soon as a stop was requested."""
    return _STOP_EVENT.wait(max(0.0, float(delay)))


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkerHealthSnapshot:
    started_at: str = field(default_factory=_utcnow)
    status: str = "starting"
    control_plane_url: str = ""
    worker_name: str = ""
    worker_id: Optional[str] = None
    registered: bool = False
    control_plane_reachable: bool = False
    last_successful_control_plane_contact_at: Optional[str] = None
    last_loop_completed_at: Optional[str] = None
    last_error: Optional[str] = None
    last_processed_jobs: int = 0
    run_once: bool = False
    poll_interval_seconds: float = 30.0
    runtime_kind: str = "docker"
    capabilities: list[str] = field(default_factory=lambda: ["docker"])


class WorkerHealthState:
    def __init__(self, control_plane_url: str, worker_name: str):
        self._lock = Lock()
        self._snapshot = WorkerHealthSnapshot(
            control_plane_url=control_plane_url,
            worker_name=worker_name,
        )

    def set_runtime(
        self,
        run_once: bool,
        poll_interval_seconds: float,
        runtime_kind: str = "docker",
        capabilities: Optional[list[str]] = None,
    ):
        with self._lock:
            self._snapshot.run_once = run_once
            self._snapshot.poll_interval_seconds = poll_interval_seconds
            self._snapshot.runtime_kind = runtime_kind
            self._snapshot.capabilities = list(capabilities or [runtime_kind])

    def set_connection(self, control_plane_url: str, worker_name: str):
        with self._lock:
            self._snapshot.control_plane_url = control_plane_url
            self._snapshot.worker_name = worker_name

    def record_control_plane_success(self, worker_id: str, processed_jobs: Optional[int] = None):
        with self._lock:
            self._snapshot.worker_id = worker_id
            self._snapshot.registered = True
            self._snapshot.control_plane_reachable = True
            self._snapshot.last_successful_control_plane_contact_at = _utcnow()
            self._snapshot.last_error = None
            self._snapshot.status = "ok"
            if processed_jobs is not None:
                self._snapshot.last_processed_jobs = processed_jobs

    def record_control_plane_failure(self, error: str):
        with self._lock:
            self._snapshot.control_plane_reachable = False
            self._snapshot.last_error = error
            self._snapshot.status = "degraded" if self._snapshot.registered else "starting"

    def record_not_ready(self, error: str):
        with self._lock:
            self._snapshot.control_plane_reachable = False
            self._snapshot.last_error = error
            self._snapshot.status = "not_ready"

    def record_loop_completed(self):
        with self._lock:
            self._snapshot.last_loop_completed_at = _utcnow()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            payload = asdict(self._snapshot)
        payload["ok"] = self._snapshot.status == "ok"
        payload["note"] = (
            "Worker process is alive; control_plane_reachable only indicates the latest known contact with the Control Plane"
        )
        return payload


class WorkerHealthHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, state: WorkerHealthState):
        super().__init__(server_address, RequestHandlerClass)
        self.state = state


class WorkerHealthRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/healthz", "/healthz/", "/health", "/health/"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        payload = json.dumps(self.server.state.snapshot()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


def _labels_from_env() -> dict:
    raw = os.environ.get("WORKER_LABELS", "{}").strip()
    if not raw:
        return {}
    try:
        labels = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("WORKER_LABELS is not valid JSON; ignoring")
        return {}
    if not isinstance(labels, dict):
        logger.warning("WORKER_LABELS must be a JSON object; ignoring")
        return {}
    return labels


_SUPPORTED_RUNTIME_KINDS = frozenset({"docker", "kubernetes", "native"})


def _worker_runtime_kind() -> str:
    raw = (os.environ.get("WORKER_RUNTIME") or "docker").strip().lower()
    if raw in _SUPPORTED_RUNTIME_KINDS:
        return raw
    logger.warning("WORKER_RUNTIME is unsupported; using Docker runtime")
    return "docker"


def _runtime_labels(labels: dict, runtime_kind: str) -> dict:
    """Merge user labels without allowing them to spoof runtime identity."""
    result = dict(labels)
    result.update(
        {
            "runtime_kind": runtime_kind,
            "runtime_type": runtime_kind,
            "supported_runtimes": runtime_kind,
            "capabilities": json.dumps([runtime_kind], separators=(",", ":")),
        }
    )
    return result


def _build_runtime(runtime_kind: str, config: WorkerAgentConfig):
    if runtime_kind == "kubernetes":
        return KubernetesRuntimeAdapter(
            namespace=os.environ.get("WORKER_KUBERNETES_NAMESPACE") or None,
            worker_id=config.worker_id,
        )
    if runtime_kind == "native":
        return NativeRuntimeAdapter()
    return DockerRuntimeAdapter()


def _resolve_live_helper_image() -> str:
    for variable in ("LIVE_FILE_HELPER_IMAGE", "WORKER_IMAGE"):
        image = (os.environ.get(variable) or "").strip()
        if image:
            return image

    for variable in ("WORKER_VERSION", "APP_VERSION"):
        version = (os.environ.get(variable) or "").strip()
        if re.fullmatch(r"\d+\.\d+\.\d+", version):
            return f"ghcr.io/danielrondongarcia/vaultline-worker:{version}"

    return "docker-volume-backup-worker-local:dev"


def _resolve_worker_version() -> str:
    worker_version = (os.environ.get("WORKER_VERSION") or "").strip()
    app_version = (os.environ.get("APP_VERSION") or "").strip()
    if worker_version and worker_version not in ("ghcr", "docker", "dev", "latest"):
        return worker_version
    if app_version and app_version not in ("dev", "latest"):
        return app_version
    if BUILD_VERSION and BUILD_VERSION not in ("dev", "latest"):
        return BUILD_VERSION
    return worker_version or app_version or BUILD_VERSION or "dev"


def _bounded_interval(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s is invalid; using %.3fs", name, default)
        return default
    if not math.isfinite(value) or value < minimum or value > maximum:
        logger.warning("%s is outside %.3f..%.3fs; using %.3fs", name, minimum, maximum, default)
        return default
    return value


def _runtime_orphan_sweep_interval() -> float:
    return _bounded_interval(
        "WORKER_RUNTIME_ORPHAN_SWEEP_INTERVAL_SECONDS",
        15.0,
        5.0,
        3600.0,
    )


_WORKER_ERROR_DETAIL_LIMIT = 512


def _bounded_worker_error_detail(error: Exception) -> str:
    detail = " ".join(str(error).split())
    detail = re.sub(
        r"(?i)(?:https?|http\+docker)://[^\s)]+",
        lambda match: (
            "http+docker://<redacted>"
            if match.group(0).lower().startswith("http+docker://")
            else "<url>"
        ),
        detail,
    )
    detail = re.sub(
        r"(?i)\b(?:authorization|bearer|token|secret|password|credential)\b\s*[:=]\s*[^\s,;]+",
        "<redacted>",
        detail,
    )
    if len(detail) <= _WORKER_ERROR_DETAIL_LIMIT:
        return detail
    return f"{detail[:_WORKER_ERROR_DETAIL_LIMIT - 3]}..."


def _is_docker_runtime_error(error: Exception) -> bool:
    qualified_type = f"{error.__class__.__module__}.{error.__class__.__name__}".lower()
    detail = str(error).lower()
    return (
        "docker.errors" in qualified_type
        or "http+docker://" in detail
        or "docker daemon" in detail
        or "docker socket" in detail
        or "no such image" in detail
        or "docker api" in detail
    )


def _is_control_plane_connectivity_error(error: Exception) -> bool:
    if isinstance(error, (URLError, socket.gaierror, ConnectionError, TimeoutError, ssl.SSLError)):
        return True
    detail = str(error).lower()
    return any(
        marker in detail
        for marker in (
            "name or service not known",
            "temporary failure in name resolution",
            "nodename nor servname",
            "getaddrinfo failed",
            "connection refused",
            "connection reset",
            "connection aborted",
            "tls handshake",
            "ssl error",
            "timed out",
        )
    )


def _classify_worker_loop_error(error: Exception) -> str:
    if isinstance(error, (ControlPlaneHTTPError, HTTPError)):
        return "control_plane_rejection"
    if _is_docker_runtime_error(error):
        return "docker_runtime"
    if _is_control_plane_connectivity_error(error):
        return "control_plane_connectivity"
    return "worker_loop"


def _format_worker_loop_error(error: Exception) -> str:
    category = _classify_worker_loop_error(error)
    if category == "control_plane_rejection":
        return str(error)
    detail = _bounded_worker_error_detail(error) or error.__class__.__name__
    if category == "control_plane_connectivity":
        return f"Worker could not connect to the Control Plane: {detail}"
    if category == "docker_runtime":
        return f"Worker reached the Control Plane but the Docker runtime failed: {detail}"
    return f"Worker loop failed ({error.__class__.__name__}): {detail}"


def _poll_worker(service: WorkerAgentService):
    interactive_poll = getattr(service, "poll_interactive_once", None)
    if callable(interactive_poll):
        try:
            return interactive_poll()
        except (AttributeError, NotImplementedError):
            logger.debug("Interactive worker lane unavailable; falling back to durable polling")
    return service.poll_once()


def start_live_lane(service: WorkerAgentService, interval: float):
    poll = getattr(service, "poll_live_once", None)
    if not callable(poll): return None
    stop = Event()
    def run():
        while not stop.is_set():
            try:
                poll()
            except Exception as exc:
                logger.debug("Live worker lane failed (error_type=%s)", exc.__class__.__name__)
            stop.wait(interval)
    thread = Thread(target=run, daemon=True, name="worker-live-lane"); thread.start(); return stop, thread


def build_service() -> WorkerAgentService:
    credential_file = resolve_credential_file()
    recovery_file = os.environ.get("WORKER_JOB_RECOVERY_FILE") or str(
        credential_file.with_name("worker_job_recovery.json")
    )
    # Environment always wins; worker.json written by `enroll` is the fallback.
    enrollment_config = load_enrollment_config(resolve_enrollment_config_file())
    credential_store = WorkerCredentialStore(credential_file)
    # The identity is re-resolved on every loop iteration (see main); this initial
    # load only seeds the config for workers that are already enrolled.
    stored = credential_store.load()
    config = WorkerAgentConfig(
        control_plane_url=os.environ.get("CONTROL_PLANE_URL")
        or enrollment_config.get("control_plane_url")
        or "http://127.0.0.1:8080",
        name=os.environ.get("WORKER_NAME") or enrollment_config.get("worker_name") or socket.gethostname(),
        host_name=os.environ.get("WORKER_HOST_NAME", socket.gethostname()),
        version=_resolve_worker_version(),
        worker_id=os.environ.get("WORKER_ID") or (stored.worker_id if stored else None),
        labels=_labels_from_env(),
        backup_runtime_image=os.environ.get(
            "BACKUP_RUNTIME_IMAGE",
            "ghcr.io/danielrondongarcia/vaultline",
        ),
        enrollment_token=os.environ.get("WORKER_ENROLLMENT_TOKEN") or os.environ.get("WORKER_SECRET") or None,
        live_helper_image=_resolve_live_helper_image(),
    )
    client = ControlPlaneClient(
        config.control_plane_url,
        ca_file=os.environ.get("CONTROL_PLANE_CA_FILE") or enrollment_config.get("control_plane_ca_file") or None,
        credential_store=credential_store,
        worker_id=config.worker_id,
        enrollment_secret=config.enrollment_token,
    )
    runtime_kind = _worker_runtime_kind()
    runtime = _build_runtime(runtime_kind, config)
    config.labels = _runtime_labels(config.labels, runtime_kind)
    snapshot_cache = RedisSnapshotCache.from_env()
    return WorkerAgentService(
        config,
        client,
        runtime,
        snapshot_cache=snapshot_cache,
        recovery_file=recovery_file,
    )


def start_health_server(state: WorkerHealthState):
    host = os.environ.get("WORKER_HEALTH_HOST", "0.0.0.0")
    port = int(os.environ.get("WORKER_HEALTH_PORT", "8081"))
    if port <= 0:
        logger.info("Worker health endpoint disabled because WORKER_HEALTH_PORT=%s", port)
        return None

    try:
        server = WorkerHealthHTTPServer((host, port), WorkerHealthRequestHandler, state=state)
    except OSError as exc:
        logger.warning("Could not bind worker health endpoint on %s:%s: %s", host, port, exc)
        return None
    Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Worker health endpoint listening on %s:%s", host, port)
    return server


def _apply_enrollment_config(service: WorkerAgentService, health_state: Optional[WorkerHealthState] = None) -> None:
    """Re-read ``worker.json`` once enrollment is detected at runtime.

    ``build_service()`` resolved the connection settings before ``enroll`` ran, so
    an idle daemon would otherwise keep the default URL. Environment always wins:
    each value is applied only when its environment variable is not set.
    """

    enrollment_config = load_enrollment_config(resolve_enrollment_config_file())
    config = service.config
    client = getattr(service, "control_plane_client", None)
    control_plane_url = None if os.environ.get("CONTROL_PLANE_URL") else enrollment_config.get("control_plane_url")
    ca_file = None if os.environ.get("CONTROL_PLANE_CA_FILE") else enrollment_config.get("control_plane_ca_file")
    worker_name = None if os.environ.get("WORKER_NAME") else enrollment_config.get("worker_name")
    if control_plane_url:
        config.control_plane_url = control_plane_url
    if worker_name:
        config.name = worker_name
    if client is not None and (control_plane_url or ca_file):
        reconfigure = getattr(client, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(base_url=control_plane_url, ca_file=ca_file)
        else:
            if control_plane_url:
                client.base_url = control_plane_url.rstrip("/")
            if ca_file:
                client.ca_file = ca_file
    if health_state is not None:
        health_state.set_connection(config.control_plane_url, config.name)
    logger.info("Enrollment detected; using Control Plane %s", config.control_plane_url)


def _enrollment_files_are_trusted(store) -> bool:
    """Defense in depth before adopting enrollment files found at runtime.

    The service runs as LocalSystem/root; a credential or ``worker.json``
    owned by anyone other than SYSTEM/Administrators (root/the current user on
    POSIX) could redirect it to an attacker-controlled Control Plane.
    """

    # Developer-mode direct credential stores are intentionally not subject to
    # machine-service ownership rules. Frozen services and explicitly managed
    # state directories must fail closed before adopting newly written files.
    if not state_dir_is_managed() and os.environ.get("WORKER_SERVICE_MODE") != "true":
        return True
    credential_path = getattr(store, "path", None)
    paths = [Path(credential_path)] if credential_path else []
    paths.append(resolve_enrollment_config_file())
    if enrollment_files_trusted(paths):
        return True
    directory = str(paths[0].parent)
    if directory not in _UNTRUSTED_ENROLLMENT_DIRS:
        _UNTRUSTED_ENROLLMENT_DIRS.add(directory)
        logger.error(
            "refusing untrusted enrollment files in %s: they must be owned by SYSTEM/Administrators (root on Linux); "
            "re-run 'vaultline-worker enroll' from an elevated shell",
            directory,
        )
    return False


def _resolve_worker_identity(service: WorkerAgentService, health_state: Optional[WorkerHealthState] = None) -> bool:
    """Refresh ``worker_id`` from the credential store on every loop.

    Returns False only when the worker is positively unenrolled: no worker id is
    known yet, a credential store is reachable, it holds no credential, and no V1
    enrollment token is configured. Any other situation (worker id already set,
    no store to inspect, unreadable credential, token present) returns True so
    ``ensure_registered`` decides and real failures keep the startup retry cap.

    The first time a stored credential is found, ``worker.json`` is re-applied so
    the connection settings written by ``enroll`` take effect without a restart.
    A credential whose files fail the ownership check is never adopted: the
    worker stays idle as if it were unenrolled.
    """

    config = service.config
    client = getattr(service, "control_plane_client", None)
    if getattr(config, "worker_id", None):
        return True
    store = getattr(client, "credential_store", None)
    if store is None or not callable(getattr(store, "load", None)):
        return True
    try:
        stored = store.load()
    except Exception:
        return True
    stored_worker_id = getattr(stored, "worker_id", None)
    if isinstance(stored_worker_id, str) and stored_worker_id:
        if not _enrollment_files_are_trusted(store):
            return False
        _apply_enrollment_config(service, health_state)
        config.worker_id = stored_worker_id
        if client is not None and not getattr(client, "worker_id", None):
            client.worker_id = stored_worker_id
        return True
    return bool(getattr(config, "enrollment_token", None))


def main():
    service = build_service()
    run_once = os.environ.get("WORKER_RUN_ONCE", "true").strip().lower() in ("1", "true", "yes", "on")
    poll_interval = _bounded_interval("WORKER_POLL_INTERVAL_SECONDS", 30.0, 0.1, 86400.0)
    interactive_interval = _bounded_interval("WORKER_INTERACTIVE_POLL_INTERVAL_SECONDS", 0.5, 0.1, 5.0)
    inventory_interval = _bounded_interval("WORKER_INVENTORY_SYNC_INTERVAL_SECONDS", poll_interval, 0.5, 86400.0)
    orphan_sweep_interval = _runtime_orphan_sweep_interval()
    last_logged_worker_id = None
    attempts = 0
    registered_once = False
    next_orphan_sweep_at = None
    last_heartbeat_at = 0.0
    last_inventory_sync_at = 0.0
    health_state = WorkerHealthState(
        control_plane_url=service.config.control_plane_url,
        worker_name=service.config.name,
    )
    runtime_kind = getattr(getattr(service, "runtime", None), "runtime_kind", "docker")
    if runtime_kind not in _SUPPORTED_RUNTIME_KINDS:
        runtime_kind = "docker"
    health_state.set_runtime(
        run_once=run_once,
        poll_interval_seconds=poll_interval,
        runtime_kind=runtime_kind,
        capabilities=[runtime_kind],
    )
    start_health_server(health_state)
    live_lane = start_live_lane(service, interactive_interval)
    idle_cycles = 0

    while not _STOP_EVENT.is_set():
        if not _resolve_worker_identity(service, health_state):
            # Idle until an administrator runs `enroll`; this is not a failure and
            # never counts against the startup retry cap.
            health_state.record_not_ready(_WAITING_FOR_ENROLLMENT)
            health_state.record_loop_completed()
            if run_once:
                raise RuntimeError("worker is not enrolled")
            if idle_cycles % _IDLE_LOG_EVERY_CYCLES == 0:
                logger.warning(
                    "Waiting for enrollment: run 'vaultline-worker enroll --control-plane-url <url>' "
                    "on this host; the daemon re-checks the credential store every %ss",
                    poll_interval,
                )
            idle_cycles += 1
            if _wait_for_stop(poll_interval):
                break
            continue
        idle_cycles = 0
        try:
            worker_id = service.ensure_registered()
            registered_once = True
            if last_logged_worker_id != worker_id:
                logger.info("Worker registered with id %s", worker_id)
                last_logged_worker_id = worker_id
            now = time.monotonic()
            if next_orphan_sweep_at is None or now >= next_orphan_sweep_at:
                next_orphan_sweep_at = now + orphan_sweep_interval
                try:
                    sweep = service.cleanup_orphaned_runtime_containers()
                    sweep = sweep if isinstance(sweep, dict) else {}
                    logger.info(
                        "Runtime orphan sweep %s: inspected=%s removed=%s failed=%s skipped=%s; "
                        "expired in-progress leases await worker confirmation",
                        "partial" if sweep.get("error") else "complete",
                        sweep.get("inspected", 0),
                        sweep.get("removed", 0),
                        sweep.get("failed", 0),
                        sweep.get("skipped", 0),
                    )
                except Exception as exc:
                    logger.warning(
                        "Runtime orphan sweep failed; expired in-progress leases await worker confirmation "
                        "(error_type=%s)",
                        exc.__class__.__name__,
                    )
            now = time.monotonic()
            if run_once or now - last_heartbeat_at >= poll_interval:
                service.send_heartbeat()
                last_heartbeat_at = now
            if run_once or now - last_inventory_sync_at >= inventory_interval:
                service.sync_inventory()
                last_inventory_sync_at = now
            processed = _poll_worker(service)
            health_state.record_control_plane_success(worker_id=worker_id, processed_jobs=len(processed))
            logger.info("Worker processed %s jobs", len(processed))
            attempts = 0
        except Exception as exc:
            attempts += 1
            error_message = _format_worker_loop_error(exc)
            health_state.record_control_plane_failure(error_message)
            logger.warning("%s", error_message)
            if attempts >= 5 and not registered_once:
                health_state.record_not_ready(error_message)
                raise RuntimeError("worker startup failed after 5 attempts") from None
        finally:
            health_state.record_loop_completed()
        if run_once and attempts == 0:
            break
        if attempts or not run_once:
            retry_delay = _bounded_interval(
                "WORKER_STARTUP_RETRY_DELAY_SECONDS",
                poll_interval,
                0.0,
                86400.0,
            ) if attempts else interactive_interval
            if _wait_for_stop(retry_delay):
                break
    if live_lane:
        live_lane[0].set()


if __name__ == "__main__":
    main()
