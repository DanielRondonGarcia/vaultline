"""Restic metadata inspection without restore-target side effects."""

import os
import re
import subprocess
import threading
from typing import Any

from src.app.application.ports.ports import ResticMetadataInspectorPort
from src.app.domain.restore_metadata import (
    MAX_DISTINCT_OWNERS,
    MAX_FILE_SAMPLES,
    MAX_HEADER_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_RECORDS,
    MAX_RECORD_BYTES,
    ResticMetadataError,
    ResticMetadataEvidence,
    ResticMetadataStreamParser,
)
from src.app.domain.restore_ownership import RestoreOwnershipPolicy
from src.restic_executable import resolve_restic_executable, restic_subprocess_environment

MAX_STDERR_TAIL_BYTES = 16 * 1024
_STDERR_SECRET = re.compile(r"(?i)(password|passphrase|secret|token|credential)(\s*[:=]\s*)[^\s,;]+")


def _value(config: Any, *names: str) -> Any:
    for name in names:
        value = config.get(name) if isinstance(config, dict) else getattr(config, name, None)
        if value is not None: return value
    return None


def _drain_stderr(stream, tail: bytearray, max_bytes: int, errors: list[Exception]) -> None:
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read(4096)
            if chunk in (b"", ""):
                return
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            tail.extend(chunk)
            if len(tail) > max_bytes:
                del tail[:len(tail) - max_bytes]
    except (OSError, ValueError) as exc:
        errors.append(exc)


def _terminate_and_reap(process) -> None:
    try:
        poll = getattr(process, "poll", None)
        if not callable(poll) or poll() is None:
            process.terminate()
    except (OSError, ProcessLookupError, AttributeError):
        pass
    try:
        process.wait(timeout=5)
    except TypeError:
        try:
            process.wait()
        except (OSError, subprocess.SubprocessError):
            pass
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except (OSError, ProcessLookupError, AttributeError):
            pass
        try:
            process.wait()
        except (OSError, subprocess.SubprocessError):
            pass
    except (OSError, subprocess.SubprocessError, AttributeError):
        pass


def _stderr_failure_detail(return_code: int, tail: bytearray) -> str:
    detail = f"restic ls --long failed with exit status {return_code}"
    if not tail:
        return detail
    text = tail.decode("utf-8", errors="replace")
    text = _STDERR_SECRET.sub(r"\1=<redacted>", text)
    text = " ".join(text.split())[-512:]
    return f"{detail}: {text}" if text else detail


class ResticMetadataInspectorAdapter(ResticMetadataInspectorPort):
    def __init__(
        self,
        process_factory=None,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        max_records: int = MAX_RECORDS,
        max_header_bytes: int = MAX_HEADER_BYTES,
        max_record_bytes: int = MAX_RECORD_BYTES,
        max_stderr_bytes: int = MAX_STDERR_TAIL_BYTES,
    ):
        self.process_factory = process_factory or subprocess.Popen
        self.max_output_bytes = max_output_bytes
        self.max_records = max_records
        self.max_header_bytes = max_header_bytes
        self.max_record_bytes = max_record_bytes
        self.max_stderr_bytes = max_stderr_bytes

    def inspect(self, snapshot: str, config: Any = None, scopes=None, policy: Any = None) -> ResticMetadataEvidence:
        process = None
        process_waited = False
        stderr_thread = None
        stderr_stream = None
        try:
            selected = policy or _value(config, "restore_ownership") or RestoreOwnershipPolicy()
            if not isinstance(selected, RestoreOwnershipPolicy): selected = RestoreOwnershipPolicy.from_dict(selected)
            selected.require_confirmation()
            env = os.environ.copy()
            repository, password = _value(config, "restic_repository", "repository"), _value(config, "restic_password", "password")
            if repository is not None: env["RESTIC_REPOSITORY"] = str(repository)
            if password is not None: env["RESTIC_PASSWORD"] = str(password)
            env = restic_subprocess_environment(env)
            process = self.process_factory(
                [resolve_restic_executable(), "ls", "--long", snapshot],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            stderr_tail = bytearray()
            stderr_errors: list[Exception] = []
            stderr_stream = getattr(process, "stderr", None)
            stderr_thread = threading.Thread(
                target=_drain_stderr,
                args=(stderr_stream, stderr_tail, self.max_stderr_bytes, stderr_errors),
                daemon=True,
            )
            stderr_thread.start()

            parser = ResticMetadataStreamParser(
                snapshot=snapshot,
                creator=_value(config, "snapshot_creator", "creator"),
                policy=selected,
                max_output_bytes=self.max_output_bytes,
                max_records=self.max_records,
                max_header_bytes=self.max_header_bytes,
                max_record_bytes=self.max_record_bytes,
                max_file_samples=MAX_FILE_SAMPLES,
                max_distinct_owners=MAX_DISTINCT_OWNERS,
                decode_errors="replace",
            )
            stdout = getattr(process, "stdout", None)
            if stdout is None:
                raise ResticMetadataError("Restic metadata inspection output is incomplete")
            while True:
                raw_line = stdout.readline(self.max_record_bytes + 2)
                if raw_line in (b"", ""):
                    break
                parser.feed(raw_line)

            return_code = process.wait()
            process_waited = True
            stderr_thread.join(timeout=5)
            if stderr_thread.is_alive():
                raise OSError("restic ls --long stderr did not reach end of stream")
            if stderr_errors:
                raise OSError("restic ls --long stderr could not be read") from stderr_errors[0]
            if not isinstance(return_code, int):
                return_code = getattr(process, "returncode", 1)
            if return_code != 0:
                return ResticMetadataEvidence(
                    snapshot=snapshot,
                    category="restic_metadata_inspection_failed",
                    detail=_stderr_failure_detail(int(return_code or 1), stderr_tail),
                )
            return parser.finish()
        except ResticMetadataError as exc:
            return ResticMetadataEvidence(snapshot=snapshot, category=exc.category, detail=str(exc))
        except ValueError as exc:
            return ResticMetadataEvidence(snapshot=snapshot, category=getattr(exc, "category", "restic_metadata_inspection_failed"), detail=str(exc))
        except (OSError, subprocess.SubprocessError):
            return ResticMetadataEvidence(snapshot=snapshot, category="restic_metadata_inspection_failed", detail="restic ls --long could not run")
        finally:
            if process is not None and not process_waited:
                _terminate_and_reap(process)
            if stderr_thread is not None:
                stderr_thread.join(timeout=5)
                if stderr_thread.is_alive() and stderr_stream is not None:
                    try:
                        stderr_stream.close()
                    except (OSError, ValueError):
                        pass
                    stderr_thread.join(timeout=1)
            if process is not None:
                for name in ("stdout", "stderr"):
                    stream = getattr(process, name, None)
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            pass


ResticMetadataInspector = ResticMetadataInspectorAdapter
