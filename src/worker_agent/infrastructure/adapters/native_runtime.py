import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterable, List

from src.worker_agent.application.policies.runtime_command_policy import RuntimeCommandPolicy
from src.worker_agent.application.ports.runtime_port import RuntimePort
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
from src.worker_agent.infrastructure.adapters.runtime_watchdog import RuntimeWatchdog
from src.worker_agent.infrastructure.security.execution_log_capture import (
    ExecutionLogCapture,
    ExecutionLogCaptureUnavailable,
)
from src.worker_agent.infrastructure.security.execution_log_recipe_stream import (
    encode_recipe_frame,
)
from src.restic_executable import (
    resolve_restic_executable,
    restic_command_with_backend_options,
    restic_subprocess_environment,
)
from src.rclone_executable import resolve_rclone_executable

MAX_RESOLVED_FILES = 32
MAX_RESOLVED_FILE_BYTES = 1024 * 1024
MAX_RESOLVED_FILES_BYTES = 4 * 1024 * 1024

logger = logging.getLogger(__name__)


def _platform_is_windows() -> bool:
    return os.name == "nt"


def _current_windows_user_sid() -> str:
    result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], check=True, capture_output=True, text=True)
    import csv
    import io
    rows = list(csv.reader(io.StringIO(result.stdout)))
    sid = rows[0][-1].strip() if rows and rows[0] else ""
    if not sid.startswith("S-"):
        raise PermissionError("could not resolve current Windows account SID")
    return sid


class NativeRuntimeAdapter(RuntimePort):
    """Run backup/runtime commands directly on the worker host."""

    runtime_kind = "native"
    MAX_RUNTIME_TIMEOUT_SECONDS = DockerRuntimeAdapter.MAX_RUNTIME_TIMEOUT_SECONDS
    DEFAULT_IDLE_TIMEOUT_SECONDS = DockerRuntimeAdapter.DEFAULT_IDLE_TIMEOUT_SECONDS
    WAIT_POLL_INTERVAL_SECONDS = 0.1
    MAX_LOG_BYTES = DockerRuntimeAdapter.MAX_LOG_BYTES
    MAX_DUMP_BYTES = DockerRuntimeAdapter.MAX_DUMP_BYTES
    MAX_ZIP_BYTES = DockerRuntimeAdapter.MAX_ZIP_BYTES
    MAX_SNAPSHOT_ENTRIES = DockerRuntimeAdapter.MAX_SNAPSHOT_ENTRIES
    MAX_SNAPSHOT_STATS_VALUE = DockerRuntimeAdapter.MAX_SNAPSHOT_STATS_VALUE
    MAX_TARGET_STATS_FIELDS = DockerRuntimeAdapter.MAX_TARGET_STATS_FIELDS
    _TARGET_STATS_MODES = DockerRuntimeAdapter._TARGET_STATS_MODES
    _STATS_FIELD_PATTERN = DockerRuntimeAdapter._STATS_FIELD_PATTERN
    _SECRET_ENV_MARKERS = DockerRuntimeAdapter._SECRET_ENV_MARKERS
    # Persistent execution-log capture (S1-S6): the bounded drain time before
    # the job result is returned and the safe CP-visible warning prefix. The
    # durable root/state configuration is shared with the Docker draft so
    # both runtimes write under the same configured worker state directory.
    EXECUTION_LOG_DRAIN_SECONDS = DockerRuntimeAdapter.EXECUTION_LOG_DRAIN_SECONDS
    EXECUTION_LOG_WARNING_PREFIX = DockerRuntimeAdapter.EXECUTION_LOG_WARNING_PREFIX
    EXECUTION_LOG_STATE_SUBDIR = DockerRuntimeAdapter.EXECUTION_LOG_STATE_SUBDIR
    EXECUTION_LOG_NAME_ATTEMPTS = DockerRuntimeAdapter.EXECUTION_LOG_NAME_ATTEMPTS
    # Bounded ephemeral recipe-frame transfer: a child that never consumes
    # the frame cannot block the job longer than this (fail-open, S4).
    RECIPE_FRAME_TRANSFER_TIMEOUT = 10.0
    _runtime_job_id = staticmethod(DockerRuntimeAdapter._runtime_job_id)
    _execution_log_root = staticmethod(DockerRuntimeAdapter._execution_log_root)

    def __init__(
        self,
        timeout_seconds: float | None = None,
        python_executable: str | None = None,
        idle_timeout_seconds: float | None = None,
    ):
        self.timeout_seconds = self._validated_timeout(
            timeout_seconds if timeout_seconds is not None else DockerRuntimeAdapter._env_timeout(
                "WORKER_RUNTIME_TIMEOUT_SECONDS"
            ),
            None,
        )
        self.idle_timeout_seconds = DockerRuntimeAdapter._validated_idle_timeout(
            idle_timeout_seconds if idle_timeout_seconds is not None else os.environ.get(
                "WORKER_RUNTIME_IDLE_TIMEOUT_SECONDS"
            ),
            self.DEFAULT_IDLE_TIMEOUT_SECONDS,
        )
        self.python_executable = python_executable or sys.executable
        self.no_lock = os.environ.get("SNAPSHOT_EXPLORER_NO_LOCK", "").strip().lower() in {"1", "true", "yes", "on"}
        # Persistent execution-log capture identity: the registered worker
        # identity is mirrored here by ensure_registered() (the same pattern
        # as the Docker adapter); the capture integration never captures
        # without a real identity.
        self.worker_id = os.environ.get("WORKER_ID") or None

    _monotonic = staticmethod(time.monotonic)

    @classmethod
    def _validated_timeout(cls, value: Any, default: float | None) -> float | None:
        """Explicit wall-clock values are validated; anything else falls back to ``default``."""
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(timeout) or timeout <= 0 or timeout > cls.MAX_RUNTIME_TIMEOUT_SECONDS:
            return default
        return timeout

    def _runtime_timeout(self, payload: Dict[str, Any]) -> float | None:
        return self._validated_timeout(payload.get("timeout_seconds"), self.timeout_seconds)

    @classmethod
    def _payload_secrets(cls, payload: Dict[str, Any]) -> set[str]:
        secrets = DockerRuntimeAdapter._collect_secret_values(payload)
        for spec in payload.get("resolved_files") or []:
            if isinstance(spec, dict) and isinstance(spec.get("content"), bytes):
                try:
                    value = spec["content"].decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if value:
                    secrets.add(value)
        return secrets

    @classmethod
    def _redact(cls, value: Any, secrets: Iterable[str] = ()) -> str:
        return DockerRuntimeAdapter._redact_text(value, set(secrets))[: cls.MAX_LOG_BYTES]

    @staticmethod
    def _callback_is_true(callback: Callable[[], bool] | None) -> bool:
        if callback is None:
            return False
        try:
            return bool(callback())
        except Exception:
            return False

    @staticmethod
    def _which(executable: str) -> str | None:
        return shutil.which(executable)

    @staticmethod
    def _native_path_parts(value: str) -> list[str]:
        import re

        return [part for part in re.split(r"[\\/]+", value) if part]

    @classmethod
    def _is_absolute_native_path(cls, value: str) -> bool:
        import re

        if value.startswith("/") and not value.startswith("//"):
            return True
        if re.match(r"^[A-Za-z]:[\\/]", value):
            return True
        if value.startswith("\\\\"):
            return len(cls._native_path_parts(value)) >= 3
        return False

    @classmethod
    def _canonical_native_path(cls, value: str) -> tuple[str, str]:
        import re

        text = value.strip()
        if re.match(r"^[A-Za-z]:[\\/]", text):
            comparable = text.replace("\\", "/").rstrip("/") or text
            return "windows", comparable.casefold()
        if text.startswith(("\\\\", "//")):
            comparable = text.replace("\\", "/").rstrip("/") or text
            return "windows", comparable.casefold()
        comparable = text.replace("\\", "/")
        while "//" in comparable and not comparable.startswith("//"):
            comparable = comparable.replace("//", "/")
        return "linux", comparable.rstrip("/") or comparable

    @classmethod
    def _validate_native_restore_destination(cls, destination: Any, source_paths: list[str]) -> None:
        if not isinstance(destination, str) or not destination.strip():
            raise ValueError("native restore requires an explicit host restore destination")
        value = destination.strip()
        if "\x00" in value or any(ord(ch) < 32 for ch in value) or not cls._is_absolute_native_path(value):
            raise ValueError("native restore destination path is invalid")
        if any(part in {".", ".."} for part in cls._native_path_parts(value)):
            raise ValueError("native restore destination traversal is not allowed")
        target_flavor, target = cls._canonical_native_path(value)
        for source in source_paths:
            source_flavor, source_norm = cls._canonical_native_path(source)
            if target_flavor == source_flavor and target != source_norm and target.startswith(source_norm.rstrip("/") + "/"):
                raise ValueError("native restore destination must not overwrite a configured source path")

    def _require_executable(self, executable: str) -> str:
        if executable == "restic" and getattr(sys, "frozen", False):
            return resolve_restic_executable()
        if executable == "rclone" and getattr(sys, "frozen", False):
            return resolve_rclone_executable()
        if self._which(executable) is None and not (os.path.isabs(executable) and os.path.exists(executable)):
            raise FileNotFoundError(
                f"required executable '{executable}' is not available on this native worker PATH"
            )
        return executable

    def _backup_engine_command(self) -> list[str]:
        if getattr(sys, "frozen", False):
            executable = sys.executable
            self._require_executable(executable)
            return [executable, "backup-engine"]
        self._require_executable(self.python_executable)
        return [self.python_executable, "-m", "src.app.main"]

    def _validate_native_scope(self, payload: Dict[str, Any]) -> list[str]:
        RuntimeCommandPolicy.validate_target_scope(payload)
        runtime = payload.get("runtime_type") or payload.get("runtime")
        if runtime is not None and str(runtime).strip().lower() != "native":
            raise ValueError("Native runtime received a non-native target")
        mode = str(payload.get("backup_mode") or "hot").strip().lower()
        if mode and mode != "hot":
            raise ValueError("native filesystem targets are hot-only")
        paths = payload.get("filesystem_paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError("native filesystem targets require explicit filesystem_paths")
        normalized = []
        for path in paths:
            if not isinstance(path, str) or not path or "\x00" in path:
                raise ValueError("filesystem_paths must contain explicit path strings")
            normalized.append(path)
        if payload.get("volumes"):
            raise ValueError("native runtime does not accept Docker volume mounts")
        if payload.get("pvc_names"):
            raise ValueError("native runtime does not accept Kubernetes PVCs")
        return normalized

    def _command_and_environment(self, payload: Dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        filesystem_paths = self._validate_native_scope(payload)
        raw_environment = payload.get("environment") if isinstance(payload.get("environment"), dict) else {}
        restore_mode = payload.get("restore_mode") or str(raw_environment.get("RESTORE_MODE", "")).strip().lower() in {"1", "true", "yes", "on"}
        if restore_mode and str(raw_environment.get("RESTORE_STOP_CONTAINERS", "")).strip().lower() in {"1", "true", "yes", "on"}:
            raise ValueError("native restore must not stop containers")
        if restore_mode:
            self._validate_native_restore_destination(raw_environment.get("RESTORE_TARGET_PATH"), filesystem_paths)
        environment = os.environ.copy()
        for key, value in raw_environment.items():
            if isinstance(key, str) and isinstance(value, (str, int, float)) and not isinstance(value, bool):
                environment[key] = str(value)
        environment["BACKUP_RUNTIME_TYPE"] = "native"
        environment["BACKUP_STOP_CONTAINERS"] = "false"
        environment["BACKUP_SOURCES_JSON"] = json.dumps(filesystem_paths, ensure_ascii=False, separators=(",", ":"))
        command = RuntimeCommandPolicy.validate(payload.get("command"))
        RuntimeCommandPolicy.validate_snapshot_scope(payload, command)
        command = RuntimeCommandPolicy.apply_lock_policy(command, self.no_lock)
        if command == ["/root/backup.sh"]:
            environment["PYTHONIOENCODING"] = "utf-8"
            return self._backup_engine_command(), environment
        if command[0] == "restic":
            environment = restic_subprocess_environment(environment)
            command = restic_command_with_backend_options(command, environment)
        command[0] = self._require_executable(command[0])
        return command, environment

    def _materialize_resolved_files(self, payload: Dict[str, Any], environment: dict[str, str]) -> str | None:
        specs = payload.get("resolved_files") or []
        if not isinstance(specs, list) or len(specs) > MAX_RESOLVED_FILES:
            raise ValueError("native runtime file secrets are invalid")
        total = 0
        parsed = []
        for spec in specs:
            if not isinstance(spec, dict):
                raise ValueError("native runtime file secrets are invalid")
            path, content = spec.get("container_path"), spec.get("content")
            if not isinstance(path, str) or not self._is_absolute_native_path(path) or "\x00" in path or any(p in {".", ".."} for p in self._native_path_parts(path)):
                raise ValueError("native runtime file secrets are invalid")
            if isinstance(content, str):
                content = content.encode("utf-8")
            if not isinstance(content, bytes) or len(content) > MAX_RESOLVED_FILE_BYTES:
                raise ValueError("native runtime file secrets are invalid")
            total += len(content)
            if total > MAX_RESOLVED_FILES_BYTES:
                raise ValueError("native runtime file secrets are invalid")
            parsed.append((spec, path, content))
        rclone_content = environment.get("RCLONE_CONF_CONTENT")
        # Strip the content value even when it originated in the worker process environment.
        environment.pop("RCLONE_CONF_CONTENT", None)
        if not parsed and rclone_content is None:
            return None
        if rclone_content is not None and not any(
            "rclone" in str(spec.get("secret_name", "")).casefold() or path.casefold().endswith("rclone.conf")
            for spec, path, _ in parsed
        ):
            if not isinstance(rclone_content, str) or len(rclone_content.encode("utf-8")) > MAX_RESOLVED_FILE_BYTES:
                raise ValueError("native runtime file secrets are invalid")
            parsed.append(({"secret_name": "rclone.conf"}, "/run/secrets/rclone.conf", rclone_content.encode("utf-8")))
        directory = tempfile.mkdtemp(prefix="worker-native-secrets-")
        try:
            if _platform_is_windows():
                user_sid = _current_windows_user_sid()
                for path in (directory,):
                    subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"*{user_sid}:F", "/grant:r", "*S-1-5-18:F", "/grant:r", "*S-1-5-32-544:F"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.chmod(directory, 0o700)
            mappings = {}
            rclone_path = None
            for index, (spec, path, content) in enumerate(parsed, start=1):
                local_path = os.path.join(directory, f"secret-{index:02d}.conf")
                with open(local_path, "xb") as handle:
                    handle.write(content)
                if _platform_is_windows():
                    subprocess.run(["icacls", local_path, "/inheritance:r", "/grant:r", f"*{user_sid}:F", "/grant:r", "*S-1-5-18:F", "/grant:r", "*S-1-5-32-544:F"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    os.chmod(local_path, 0o600)
                canonical = self._canonical_secret_path(path)
                mappings[canonical] = local_path
                if "rclone" in str(spec.get("secret_name", "")).casefold() or path.casefold().endswith("rclone.conf"):
                    rclone_path = local_path
            for key, value in list(environment.items()):
                if not isinstance(value, str):
                    continue
                try:
                    mapped = mappings.get(self._canonical_secret_path(value))
                except Exception:
                    mapped = None
                if mapped:
                    environment[key] = mapped
            if rclone_path:
                environment["RCLONE_CONFIG"] = rclone_path
            return directory
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise PermissionError("native runtime could not securely materialize file secrets") from None

    @classmethod
    def _canonical_secret_path(cls, value: str) -> tuple[str, str]:
        text = value.strip().replace("\\", "/")
        if re.match(r"^[A-Za-z]:/", text):
            text = text[2:]
        return cls._canonical_native_path(text)

    def collect_inventory(self) -> Dict[str, Any]:
        return {
            "runtime": self.runtime_kind,
            "runtime_type": self.runtime_kind,
            "native_available": True,
            "filesystem_paths": [],
        }

    def self_check(self) -> Dict[str, Any]:
        executables = {}
        for name in ("restic", "rclone", "aws", "ssh", "scp"):
            if name in {"restic", "rclone"} and getattr(sys, "frozen", False):
                try:
                    if name == "restic":
                        resolve_restic_executable()
                    else:
                        resolve_rclone_executable()
                except OSError:
                    executables[name] = False
                else:
                    executables[name] = True
            else:
                executables[name] = bool(self._which(name))
        return {"native_available": True, "runtime_type": self.runtime_kind, "executables": executables}

    def cleanup_orphaned_runtime_jobs(self, recover_callback: Callable[[Any, Dict[str, Any]], str] | None = None) -> Dict[str, Any]:
        return {"inspected": 0, "removed": 0, "failed": 0, "skipped": 0, "retained": 0, "removed_ids": [], "failed_ids": [], "retained_ids": []}

    def _execution_log_alert(
        self,
        payload: Dict[str, Any],
        reason: str,
        output_callback: Callable[[str], None] | None,
    ) -> None:
        """S4: persistent-execution-log failures continue and alert.

        The alert is a fixed, safe, actionable reason (no secret values, no
        raw exception text) visible locally (worker log) and through the
        existing job progress callback to the Control Plane.
        """
        job_id = self._runtime_job_id(payload)
        logger.warning(
            "Persistent execution log capture unavailable (job_id=%s runtime=%s reason=%s)",
            job_id,
            self.runtime_kind,
            reason,
        )
        if output_callback is None:
            return
        try:
            output_callback(
                f"\n{self.EXECUTION_LOG_WARNING_PREFIX} execution log unavailable for job {job_id}: {reason}\n"
            )
        except Exception:
            logger.debug("Execution log warning callback failed")

    @classmethod
    def _execution_log_capture_path(cls, worker_id: str, job_id: str, root: str) -> tuple:
        """Free ``(log, recipe)`` names inside the fixed structure (S1).

        Native mirror of the shared Docker policy: same-second captures of
        the same job id shift to a deterministic ``-1..-9`` sibling name
        (the foundation's exclusive-name policy) instead of colliding inside
        the child; existing logs and recipes are only observed, never
        opened, moved or rewritten. The runtime component is this adapter's
        ``native`` kind.
        """
        base = ExecutionLogCapture.job_log_path(root, worker_id, cls.runtime_kind, job_id)
        for index in range(cls.EXECUTION_LOG_NAME_ATTEMPTS):
            log_path = base if index == 0 else base.with_name(f"{base.stem}-{index}{base.suffix}")
            recipe_path = log_path.with_name(f"{log_path.name}.recipe.json")
            if not log_path.exists() and not recipe_path.exists():
                return log_path, recipe_path
        raise ExecutionLogCaptureUnavailable("execution log name could not be reserved")

    def _start_execution_log_capture(
        self,
        payload: Dict[str, Any],
        secrets: set[str],
        output_callback: Callable[[str], None] | None,
    ) -> Dict[str, Any] | None:
        """S1, S2, S5: prepare the executor-owning persistent capture.

        Writes the one-shot redaction recipe next to the per-job log path
        under the configured durable worker state root; the executor is then
        wrapped into the executor-owning relay child (S5), which owns the
        executor's own stdout/stderr pipes, forwards them verbatim to the
        worker's live streams and feeds the detached redaction-gated capture
        child, so capture continues after this worker or its live pipe dies.
        Never raises: any setup failure alerts (S4) and returns ``None`` so
        the execution itself is untouched (the command then runs directly).
        """
        worker_id = getattr(self, "worker_id", None)
        if not isinstance(worker_id, str) or not worker_id:
            self._execution_log_alert(
                payload,
                "worker identity is unavailable; register the worker before running jobs",
                output_callback,
            )
            return None
        try:
            root = self._execution_log_root()
            log_path, _recipe_path = self._execution_log_capture_path(
                worker_id, self._runtime_job_id(payload), root
            )
        except ValueError:
            self._execution_log_alert(
                payload,
                "worker or job identity is not valid for the fixed execution log structure",
                output_callback,
            )
            return None
        except ExecutionLogCaptureUnavailable:
            self._execution_log_alert(
                payload,
                "execution log name could not be reserved under the fixed structure",
                output_callback,
            )
            return None
        # Ephemeral secret transfer (S5): secrets are encoded as a bounded
        # length-prefixed frame and piped to the relay child's stdin; no
        # recipe file is ever written to disk, so a crash between preparation
        # and consumption can never leave plaintext on the host.
        try:
            recipe_frame = encode_recipe_frame(sorted(secrets))
        except Exception:
            self._execution_log_alert(
                payload,
                "redaction recipe frame could not be encoded",
                output_callback,
            )
            return None
        logger.info(
            "Persistent execution log capture started (job_id=%s worker_id=%s)",
            self._runtime_job_id(payload),
            worker_id,
        )
        # S3: opportunistically enforce the configured retention window on the
        # same persistent root (the committed sweep never raises and only
        # deletes managed job logs).
        try:
            summary = ExecutionLogCapture.prune(root)
            if isinstance(summary, dict):
                logger.debug(
                    "Execution log retention sweep (deleted=%s failed=%s)",
                    summary.get("deleted"),
                    summary.get("failed"),
                )
        except Exception as exc:
            logger.debug("Execution log retention sweep unavailable (error_type=%s)", exc.__class__.__name__)
        return {
            "log_path": log_path,
            "recipe_frame": recipe_frame,
            "root": root,
        }

    def _relay_spawn_options(self) -> Dict[str, Any]:
        """The relay must own its process group so cancellation can take down
        the executor with it, while the detached capture child (its own
        session/process group) is never targeted."""
        if os.name == "nt":
            return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        return {"start_new_session": True}

    def _relay_graceful_stop_request(self, process: Any) -> None:
        """Ask the executor-owning relay to stop its executor and exit
        (Windows: CTRL_BREAK into the relay's new process group, where both
        console group members stop; the detached capture child has no
        console and is never touched). POSIX cancellation relies on the
        relay's own SIGTERM handler and the process-group fallbacks."""
        if os.name != "nt":
            return
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int):
            return
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        except Exception:
            pass

    @staticmethod
    def _terminate_process_tree(process: Any) -> None:
        """Best-effort termination of a relay process and its own children
        (cancellation/timeout must not orphan the executor).

        The relay runs in its own session/process group, so POSIX can signal
        the whole group; on Windows the CTRL_BREAK request plus the relay's
        own stop handler is the graceful path and otherwise the executor dies
        on the broken pipe it writes into. The detached capture child holds
        its own session and is never targeted."""
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int):
            return
        try:
            if os.name == "nt":
                return
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            pass

    @staticmethod
    def _kill_process_tree(process: Any) -> None:
        """Final fallback for the relay tree (POSIX SIGKILL on its own group;
        the detached capture child is never targeted)."""
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int):
            return
        try:
            if os.name == "nt":
                return
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            pass

    def _finish_execution_log_capture(
        self,
        capture: Dict[str, Any] | None,
        payload: Dict[str, Any],
        output_callback: Callable[[str], None] | None,
        *,
        drain: bool,
    ) -> None:
        """Confirm the executor-owned evidence is on the host.

        For a job that finished on its own the relay (and through it the
        detached capture child) has already seen the executor's EOF; a
        bounded wait confirms the evidence file was created and gives the
        child time to persist the redacted evidence (never false-fails the
        job). Canceled or timed-out jobs do not wait, so their latency is
        unchanged: the detached child sees the relay's EOF and finalizes on
        its own. Never raises.
        """
        if not capture:
            return
        log_path = capture.get("log_path")
        if log_path is None:
            return
        try:
            if not drain:
                return
            exists = self._wait_for_evidence(log_path, self.EXECUTION_LOG_DRAIN_SECONDS)
            if not exists:
                self._execution_log_alert(
                    payload,
                    "execution log file could not be created",
                    output_callback,
                )
                return
            finalized = self._wait_for_completion_marker(log_path, self.EXECUTION_LOG_DRAIN_SECONDS)
            if not finalized:
                logger.info(
                    "Execution log capture still streaming after the job finished; it keeps the partial evidence separately (job_id=%s)",
                    self._runtime_job_id(payload),
                )
            # Ephemeral recipe: no file to clean up; the frame lives only in
            # the pipe buffer and the child's memory.
        except Exception:
            logger.debug("Execution log capture finalization failed", exc_info=True)

    @staticmethod
    def _wait_for_evidence(log_path: object, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if os.path.exists(os.fspath(log_path)):
                    return True
            except OSError:
                return False
            time.sleep(0.05)
        return os.path.exists(os.fspath(log_path))

    @staticmethod
    def _wait_for_completion_marker(log_path: object, timeout: float) -> bool:
        """Bounded, cheap completion check: only the log tail is read."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with open(os.fspath(log_path), "rb") as handle:
                    handle.seek(0, os.SEEK_END)
                    size = handle.tell()
                    handle.seek(max(0, size - 4096))
                    tail = handle.read()
                if b"[completed]" in tail or b"[interrupted]" in tail:
                    return True
            except OSError:
                return False
            time.sleep(0.05)
        return False

    def _cleanup_recipe_file(self, recipe_path: object) -> None:
        """Best-effort removal of the one-shot recipe once the capture child
        had its window to consume it, so a child that died before reading it
        can never leave secret-bearing bytes on the host; missing files are
        fine."""
        if not isinstance(recipe_path, (str, os.PathLike)):
            return
        try:
            os.remove(os.fspath(recipe_path))
        except OSError:
            pass

    def _run_process(
        self,
        command: list[str],
        environment: dict[str, str],
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None,
        output_callback: Callable[[str], None] | None,
        binary: bool = False,
        capture: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        secrets = self._payload_secrets(payload)
        if self._callback_is_true(cancel_check):
            return self._failure("native runtime canceled before launch", secrets, 130, binary=binary)
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=not binary,
            encoding=None if binary else "utf-8",
            errors=None if binary else "replace",
            bufsize=1 if not binary else -1,
            **((capture or {}).get("spawn_options") or {}),
        )
        # Ephemeral secret handoff: write the framed recipe to the relay
        # child's stdin and close it so the relay can consume the frame
        # before spawning the executor. A broken or slow child never blocks
        # the job: the write is best-effort and capture simply disables.
        recipe_frame = (capture or {}).get("recipe_frame")
        if recipe_frame and process.stdin is not None:
            # The frame is binary; use the underlying buffer when the stream
            # is text-mode so bytes are never written through a TextIOWrapper.
            # The write is bounded by a daemon thread so a child that never
            # consumes the frame cannot block the job (fail-open, S4).
            stdin_binary = getattr(process.stdin, "buffer", process.stdin)
            transfer_done = threading.Event()

            def _write_frame():
                try:
                    stdin_binary.write(recipe_frame)
                    stdin_binary.flush()
                except (OSError, BrokenPipeError, TypeError):
                    pass
                finally:
                    transfer_done.set()

            frame_writer = threading.Thread(target=_write_frame, daemon=True)
            frame_writer.start()
            transfer_done.wait(self.RECIPE_FRAME_TRANSFER_TIMEOUT)
            if not transfer_done.is_set():
                # The write is still blocked on a full pipe (child not
                # reading); a synchronous close would deadlock on the
                # stream lock held by the blocked write. Close from a
                # daemon thread instead (same pattern as
                # _RelayCaptureSink._close_stream_async).

                def _close_stdin_async():
                    try:
                        stdin_binary.close()
                    except Exception:
                        pass

                threading.Thread(target=_close_stdin_async, daemon=True).start()
            else:
                try:
                    process.stdin.close()
                except Exception:
                    pass
        stdout_parts: list[Any] = []
        stderr_parts: list[Any] = []
        watchdog = RuntimeWatchdog(
            self._runtime_timeout(payload), self.idle_timeout_seconds, cancel_check, clock=self._monotonic
        )

        def consume(stream, parts: list[Any], emit: bool) -> None:
            if stream is None:
                return
            while True:
                chunk = stream.readline()
                if chunk in ("", b""):
                    break
                watchdog.record_activity()
                parts.append(chunk)
                if emit and output_callback is not None:
                    safe = self._redact(chunk, secrets)
                    if safe:
                        output_callback(safe)

        stdout_thread = threading.Thread(target=consume, args=(process.stdout, stdout_parts, not binary), daemon=True)
        stderr_thread = threading.Thread(target=consume, args=(process.stderr, stderr_parts, not binary), daemon=True)
        stdout_thread.start(); stderr_thread.start()
        process_terminated = True
        cleanup_error = ""
        while True:
            stop_reason = watchdog.verdict()
            if stop_reason:
                # The relay (when capture is active) is the executor owner:
                # ask it to stop its executor first (Windows CTRL_BREAK),
                # then fall back to terminate and the process-group fallbacks
                # so the executor is never orphaned. The detached capture
                # child holds its own session and survives to finalize.
                self._relay_graceful_stop_request(process)
                try:
                    process.terminate()
                except Exception:
                    pass
                break
            try:
                return_code = process.wait(timeout=watchdog.poll_interval(self.WAIT_POLL_INTERVAL_SECONDS))
                break
            except subprocess.TimeoutExpired:
                continue
        canceled = stop_reason == RuntimeWatchdog.CANCELED
        timed_out = bool(stop_reason) and not canceled
        if stop_reason:
            self._terminate_process_tree(process)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except Exception as exc:
                    cleanup_error = f"could not kill native runtime process: {exc}"
                else:
                    self._kill_process_tree(process)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process_terminated = False
                    cleanup_error = cleanup_error or "could not confirm process termination after kill"
                except Exception as exc:
                    process_terminated = False
                    cleanup_error = cleanup_error or f"could not confirm process termination after kill: {exc}"
            except Exception as exc:
                process_terminated = False
                cleanup_error = f"could not confirm process termination: {exc}"
            return_code = 130 if canceled else 124
        stdout_thread.join(timeout=2); stderr_thread.join(timeout=2)
        timeout_error = watchdog.describe(stop_reason, "native runtime") if timed_out else ""
        if cleanup_error:
            timeout_error = f"{timeout_error}; {cleanup_error}" if timeout_error else cleanup_error
        if binary:
            stdout = b"".join(part if isinstance(part, bytes) else str(part).encode("utf-8") for part in stdout_parts)
            stderr = self._redact(b"".join(part if isinstance(part, bytes) else str(part).encode("utf-8") for part in stderr_parts), secrets)
            result = {"success": return_code == 0, "status_code": return_code, "stdout_bytes": stdout, "stderr": stderr, "canceled": canceled}
            if canceled or timed_out:
                result["process_terminated"] = process_terminated
            if timeout_error:
                result["error"] = timeout_error
            elif return_code != 0 and stderr:
                result["error"] = stderr
            return result
        logs = self._redact("".join(str(part) for part in stdout_parts), secrets)
        stderr = self._redact("".join(str(part) for part in stderr_parts), secrets)
        result = {"success": return_code == 0, "status_code": return_code, "logs": logs, "stderr": stderr, "canceled": canceled}
        if canceled or timed_out:
            result["process_terminated"] = process_terminated
        if timeout_error:
            result["error"] = timeout_error
        elif return_code != 0 and stderr:
            result["error"] = stderr
        return result

    def _failure(self, message: str, secrets: set[str], status_code: int = 1, binary: bool = False) -> Dict[str, Any]:
        safe = self._redact(message, secrets)
        result = {"success": False, "status_code": status_code, "error": safe, "canceled": status_code == 130, "stderr": safe}
        if binary:
            result["stdout_bytes"] = b""
        else:
            result["logs"] = ""
        return result

    @staticmethod
    def _is_restore_environment(environment: dict[str, str]) -> bool:
        return str(environment.get("RESTORE_MODE", "")).strip().lower() in {"1", "true", "yes", "on"}

    def run_runtime_job(self, image: str, payload: Dict[str, Any], cancel_check: Callable[[], bool] | None = None, output_callback: Callable[[str], None] | None = None) -> Dict[str, Any]:
        secrets = self._payload_secrets(payload)
        result_dir = None
        secret_dir = None
        restore_result_path = None
        capture: Dict[str, Any] | None = None
        result: Dict[str, Any] | None = None
        try:
            command, environment = self._command_and_environment(payload)
            secret_dir = self._materialize_resolved_files(payload, environment)
            if payload.get("_restore_result_transport") and self._is_restore_environment(environment):
                result_dir = tempfile.mkdtemp(prefix="worker-native-restore-result-", dir=tempfile.gettempdir())
                os.chmod(result_dir, 0o700)
                restore_result_path = os.path.join(result_dir, DockerRuntimeAdapter.RESTORE_RESULT_FILENAME)
                environment["RESTORE_RESULT_FILE"] = restore_result_path
            # Independent persistent execution-log capture (S1, S2, S5): the
            # validated executor command is wrapped into the executor-owning
            # capture relay, which owns the executor's own stdout/stderr
            # pipes, forwards them verbatim to the live streams and feeds the
            # detached redaction-gated capture child; setup failure only
            # alerts (S4) and runs the command directly.
            capture = self._start_execution_log_capture(payload, secrets, output_callback)
            if capture is not None:
                try:
                    command = list(
                        ExecutionLogCapture.capture_relay_command(
                            capture["log_path"], None, command, recipe_stream=True
                        )
                    )
                    options = self._relay_spawn_options()
                    options["stdin"] = subprocess.PIPE
                    capture["spawn_options"] = options
                except (TypeError, ValueError):
                    capture = None
                    self._execution_log_alert(
                        payload,
                        "execution log capture relay could not be prepared",
                        output_callback,
                    )
            result = self._run_process(command, environment, payload, cancel_check, output_callback, binary=False, capture=capture)
            if restore_result_path:
                restore_evidence, restore_error = DockerRuntimeAdapter._read_restore_result(restore_result_path, secrets)
                if restore_evidence:
                    result["restore_ownership"] = restore_evidence
                if restore_error:
                    result.update(self._failure(restore_error, secrets, result.get("status_code") or 1))
                    result["restore_ownership"] = restore_evidence
                elif restore_evidence and restore_evidence.get("status") != "succeeded":
                    result["success"] = False
                    result["error"] = restore_evidence.get("error") or "native restore failed"
            return result
        except Exception as exc:
            return self._failure(str(exc), secrets)
        finally:
            if result_dir:
                shutil.rmtree(result_dir, ignore_errors=True)
            if secret_dir:
                shutil.rmtree(secret_dir, ignore_errors=True)
            # Finalize the persistent capture: drained for jobs that finished
            # on their own, EOF-only for canceled/timed-out jobs (latency
            # unchanged); never raises and never changes the result.
            self._finish_execution_log_capture(
                capture,
                payload,
                output_callback,
                drain=(
                    result is not None
                    and result.get("status_code") not in (124, 130)
                ),
            )

    def run_runtime_job_binary(self, image: str, payload: Dict[str, Any], cancel_check: Callable[[], bool] | None = None) -> Dict[str, Any]:
        secrets = self._payload_secrets(payload)
        secret_dir = None
        try:
            command, environment = self._command_and_environment(payload)
            secret_dir = self._materialize_resolved_files(payload, environment)
            return self._run_process(command, environment, payload, cancel_check, None, binary=True)
        except Exception as exc:
            return self._failure(str(exc), secrets, binary=True)
        finally:
            if secret_dir:
                shutil.rmtree(secret_dir, ignore_errors=True)

    def list_restic_snapshots(self, image: str, payload: Dict[str, Any], cancel_check: Callable[[], bool] | None = None, output_callback: Callable[[str], None] | None = None) -> Dict[str, Any]:
        summary = self.run_runtime_job(image, payload, cancel_check=cancel_check, output_callback=output_callback)
        snapshots = []
        if summary.get("success"):
            try:
                parsed = json.loads(summary.get("logs") or "[]")
                snapshots = parsed if isinstance(parsed, list) else [parsed]
            except (TypeError, json.JSONDecodeError):
                summary["success"] = False
                summary["error"] = "failed to parse restic snapshots JSON"
        max_entries = DockerRuntimeAdapter._bounded_limit(payload.get("max_entries"), self.MAX_SNAPSHOT_ENTRIES, self.MAX_SNAPSHOT_ENTRIES)
        if len(snapshots) > max_entries:
            summary["success"] = False
            summary["error"] = "snapshot listing exceeded the permitted entry limit"
            snapshots = []
        summary["snapshots"] = snapshots[:max_entries]
        return summary

    project_restic_stats = staticmethod(DockerRuntimeAdapter.project_restic_stats)
    project_snapshot_stats = staticmethod(DockerRuntimeAdapter.project_snapshot_stats)

    def get_restic_stats(self, image: str, payload: Dict[str, Any], cancel_check: Callable[[], bool] | None = None, output_callback: Callable[[str], None] | None = None) -> Dict[str, Any]:
        return DockerRuntimeAdapter.get_restic_stats(self, image, payload, cancel_check=cancel_check, output_callback=output_callback)

    def get_restic_snapshot_stats(self, image: str, payload: Dict[str, Any], cancel_check: Callable[[], bool] | None = None, output_callback: Callable[[str], None] | None = None) -> Dict[str, Any]:
        return DockerRuntimeAdapter.get_restic_snapshot_stats(self, image, payload, cancel_check=cancel_check, output_callback=output_callback)
