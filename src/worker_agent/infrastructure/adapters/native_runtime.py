import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterable, List

from src.worker_agent.application.policies.runtime_command_policy import RuntimeCommandPolicy
from src.worker_agent.application.ports.runtime_port import RuntimePort
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter

MAX_RESOLVED_FILES = 32
MAX_RESOLVED_FILE_BYTES = 1024 * 1024
MAX_RESOLVED_FILES_BYTES = 4 * 1024 * 1024


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
    DEFAULT_RUNTIME_TIMEOUT_SECONDS = 1800.0
    MAX_LOG_BYTES = DockerRuntimeAdapter.MAX_LOG_BYTES
    MAX_DUMP_BYTES = DockerRuntimeAdapter.MAX_DUMP_BYTES
    MAX_ZIP_BYTES = DockerRuntimeAdapter.MAX_ZIP_BYTES
    MAX_SNAPSHOT_ENTRIES = DockerRuntimeAdapter.MAX_SNAPSHOT_ENTRIES
    MAX_SNAPSHOT_STATS_VALUE = DockerRuntimeAdapter.MAX_SNAPSHOT_STATS_VALUE
    MAX_TARGET_STATS_FIELDS = DockerRuntimeAdapter.MAX_TARGET_STATS_FIELDS
    _TARGET_STATS_MODES = DockerRuntimeAdapter._TARGET_STATS_MODES
    _STATS_FIELD_PATTERN = DockerRuntimeAdapter._STATS_FIELD_PATTERN
    _SECRET_ENV_MARKERS = DockerRuntimeAdapter._SECRET_ENV_MARKERS

    def __init__(self, timeout_seconds: float | None = None, python_executable: str | None = None):
        self.timeout_seconds = self._validated_timeout(
            timeout_seconds if timeout_seconds is not None else os.environ.get(
                "WORKER_RUNTIME_TIMEOUT_SECONDS", self.DEFAULT_RUNTIME_TIMEOUT_SECONDS
            ),
            self.DEFAULT_RUNTIME_TIMEOUT_SECONDS,
        )
        self.python_executable = python_executable or sys.executable
        self.no_lock = os.environ.get("SNAPSHOT_EXPLORER_NO_LOCK", "").strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def _validated_timeout(cls, value: Any, default: float) -> float:
        try:
            timeout = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > 24 * 60 * 60:
            return float(default)
        return timeout

    def _runtime_timeout(self, payload: Dict[str, Any]) -> float:
        return self._validated_timeout(payload.get("timeout_seconds"), float(self.timeout_seconds))

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
            if target_flavor == source_flavor and (target == source_norm or target.startswith(source_norm.rstrip("/") + "/")):
                raise ValueError("native restore destination must not overwrite a configured source path")

    def _require_executable(self, executable: str) -> None:
        if self._which(executable) is None and not (os.path.isabs(executable) and os.path.exists(executable)):
            raise FileNotFoundError(
                f"required executable '{executable}' is not available on this native worker PATH"
            )

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
            return self._backup_engine_command(), environment
        self._require_executable(command[0])
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
        executables = {name: bool(self._which(name)) for name in ("tar", "restic", "gpg", "rclone", "aws", "ssh", "scp")}
        return {"native_available": True, "runtime_type": self.runtime_kind, "executables": executables}

    def cleanup_orphaned_runtime_jobs(self, recover_callback: Callable[[Any, Dict[str, Any]], str] | None = None) -> Dict[str, Any]:
        return {"inspected": 0, "removed": 0, "failed": 0, "skipped": 0, "retained": 0, "removed_ids": [], "failed_ids": [], "retained_ids": []}

    def _run_process(
        self,
        command: list[str],
        environment: dict[str, str],
        payload: Dict[str, Any],
        cancel_check: Callable[[], bool] | None,
        output_callback: Callable[[str], None] | None,
        binary: bool = False,
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
            bufsize=1 if not binary else -1,
        )
        stdout_parts: list[Any] = []
        stderr_parts: list[Any] = []

        def consume(stream, parts: list[Any], emit: bool) -> None:
            if stream is None:
                return
            while True:
                chunk = stream.readline()
                if chunk in ("", b""):
                    break
                parts.append(chunk)
                if emit and output_callback is not None:
                    safe = self._redact(chunk, secrets)
                    if safe:
                        output_callback(safe)

        stdout_thread = threading.Thread(target=consume, args=(process.stdout, stdout_parts, not binary), daemon=True)
        stderr_thread = threading.Thread(target=consume, args=(process.stderr, stderr_parts, not binary), daemon=True)
        stdout_thread.start(); stderr_thread.start()
        canceled = False
        timed_out = False
        process_terminated = True
        cleanup_error = ""
        timeout = self._runtime_timeout(payload)
        deadline = time.monotonic() + timeout
        while True:
            if self._callback_is_true(cancel_check):
                canceled = True
                try:
                    process.terminate()
                except Exception:
                    pass
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                try:
                    process.terminate()
                except Exception:
                    pass
                break
            try:
                return_code = process.wait(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if canceled or timed_out:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except Exception as exc:
                    cleanup_error = f"could not kill native runtime process: {exc}"
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
        timeout_error = f"native runtime timed out after {timeout:g} seconds" if timed_out else ""
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
        try:
            command, environment = self._command_and_environment(payload)
            secret_dir = self._materialize_resolved_files(payload, environment)
            if payload.get("_restore_result_transport") and self._is_restore_environment(environment):
                result_dir = tempfile.mkdtemp(prefix="worker-native-restore-result-", dir=tempfile.gettempdir())
                os.chmod(result_dir, 0o700)
                restore_result_path = os.path.join(result_dir, DockerRuntimeAdapter.RESTORE_RESULT_FILENAME)
                environment["RESTORE_RESULT_FILE"] = restore_result_path
            result = self._run_process(command, environment, payload, cancel_check, output_callback, binary=False)
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
