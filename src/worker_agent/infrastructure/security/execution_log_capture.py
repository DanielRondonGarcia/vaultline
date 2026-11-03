"""Persistent, crash-resistant execution log capture for worker executors.

T1 foundation of ``odd/tasks/persistent-worker-execution-logs.md`` (S1-S6):
every executor run leaves redacted, bounded output on the host under a fixed
structure (S1) shared by Docker and native runtimes (S2) and pruned at the
default 30-day retention (S3); uncapturable failures raise
``ExecutionLogCaptureUnavailable`` so callers continue and alert (S4, wiring
in T2); ``ExecutionLogWriter`` persists only redaction-gated, exclusively
created, periodically fsynced output (S5) and the detached capture child
(``run_capture_child``) outlives the worker, reading the executor output pipe
(stdin mode) or the Docker SDK container log follow stream (``--container``
mode); Windows ACL-before-write, long-URL split redaction and frozen
dispatch are S6. Components: :class:`ExecutionLogCapture` (structure,
recipes, spawn, delegated retention),
:class:`DockerContainerLogSource` (read-only daemon stream).

Security invariants: nothing reaches disk before redaction - chunk tails and
open URL heads are held until complete or dropped fail-closed, so a secret
or URL split across chunks is never persisted half-unredacted; a corrupt or
missing recipe aborts the child before any file exists (fail-closed for raw
output, fail-open for persistence); the Docker source makes exactly two
read-only SDK calls and validates the container identifier first. The
chunk-boundary redaction gate (:class:`StreamRedactor`) and the secure
bounded log-file writer (:class:`ExecutionLogWriter`) live in the focused
sibling modules :mod:`execution_log_redaction`,
:mod:`execution_log_writer`; the one-shot secret-redaction-recipe
creation/loading and the 30-day managed-log pruning sweep live in
:mod:`execution_log_recipe` and :mod:`execution_log_retention`; all are
re-exported or delegated here to
keep the single capture surface importable both as a package module and as
a directly executed script (source-mode child from any cwd).

Frozen workers rerun the same packaged executable with the non-public
``CAPTURE_CHILD_CLI_SENTINEL`` argument (``src.worker_agent.cli`` dispatches
it before argparse); argv carries paths and fixed options only, never a
secret value. Child exit statuses: 0 completed; 1 open stream broke after
partial evidence (kept); 2 corrupt recipe or malformed arguments (no log);
3 log file unavailable; 4 daemon stream unusable (nothing persisted).
"""

from __future__ import annotations

import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

try:
    from .execution_log_paths import ExecutionLogPaths
    from .execution_log_recipe import (
        MAX_RECIPE_BYTES,
        MAX_RECIPE_ITEMS,
        load_redaction_recipe,
        write_redaction_recipe,
    )
    from .execution_log_redaction import StreamRedactor
    from .execution_log_recipe_stream import (
        RecipeFrameError,
        encode_recipe_frame,
        read_recipe_frame,
    )
    from .execution_log_retention import ExecutionLogRetention
    from .execution_log_writer import (
        FSYNC_INTERVAL_BYTES,
        MAX_LOG_BYTES,
        ExecutionLogCaptureUnavailable,
        ExecutionLogWriter,
        current_windows_user_sid,
        restrict_windows_acl,
    )
except ImportError:  # direct script execution (source-mode child): the
    # containing directory is sys.path[0], so the sibling import works.
    from execution_log_paths import ExecutionLogPaths  # type: ignore[no-redef]
    from execution_log_recipe import (  # type: ignore[no-redef]
        MAX_RECIPE_BYTES,
        MAX_RECIPE_ITEMS,
        load_redaction_recipe,
        write_redaction_recipe,
    )
    from execution_log_redaction import StreamRedactor  # type: ignore[no-redef]
    from execution_log_recipe_stream import (  # type: ignore[no-redef]
        RecipeFrameError,
        encode_recipe_frame,
        read_recipe_frame,
    )
    from execution_log_retention import ExecutionLogRetention  # type: ignore[no-redef]
    from execution_log_writer import (  # type: ignore[no-redef]
        FSYNC_INTERVAL_BYTES,
        MAX_LOG_BYTES,
        ExecutionLogCaptureUnavailable,
        ExecutionLogWriter,
        current_windows_user_sid,
        restrict_windows_acl,
    )

# Non-public internal CLI argument running the detached capture child in
# packaged (frozen) workers; the packaged executable re-invokes itself as
# ``<exe> __execution-log-capture --capture <log> --recipe <recipe>`` and the
# worker CLI dispatches it before argparse.
CAPTURE_CHILD_CLI_SENTINEL = "__execution-log-capture"

CAPTURE_CHILD_EXIT_RECIPE_FAILURE = 2
CAPTURE_CHILD_EXIT_UNAVAILABLE = 3
CAPTURE_CHILD_EXIT_DAEMON_FAILURE = 4
READ_CHUNK_BYTES = 65536
# S5: the executor-owning relay bounds how long it may wait for the detached
# capture child to drain after the executor exits (same value the adapters
# drain with), so relay exit can never block the job result unboundedly.
RELAY_CAPTURE_DRAIN_SECONDS = 30.0
RELAY_SINK_QUEUE_DEPTH = 128

try:
    import docker
except ModuleNotFoundError:
    docker = None


class ExecutionLogCapture:
    """Fixed host location and lifecycle helpers for persistent job logs."""

    # Fixed-location, path-generation and retention-policy constants are
    # delegated to the committed sibling path policy (the single source);
    # the writer bounds are delegated to the sibling writer module. The
    # capture module keeps only its own capture-runtime bounds below.
    ROOT_ENV = ExecutionLogPaths.ROOT_ENV
    RETENTION_ENV = ExecutionLogPaths.RETENTION_ENV
    # Module-level mirror kept for callers dispatching the frozen entrypoint.
    CAPTURE_CHILD_CLI_SENTINEL = CAPTURE_CHILD_CLI_SENTINEL
    RUNTIME_KINDS = ExecutionLogPaths.RUNTIME_KINDS
    DEFAULT_RETENTION_DAYS = ExecutionLogPaths.DEFAULT_RETENTION_DAYS
    MIN_RETENTION_DAYS = ExecutionLogPaths.MIN_RETENTION_DAYS
    MAX_RETENTION_DAYS = ExecutionLogPaths.MAX_RETENTION_DAYS
    MAX_LOG_BYTES = MAX_LOG_BYTES  # noqa: F811 sibling-writer bound alias
    FSYNC_INTERVAL_BYTES = FSYNC_INTERVAL_BYTES  # noqa: F811
    MAX_RECIPE_ITEMS = MAX_RECIPE_ITEMS  # noqa: F811 sibling-recipe bound alias
    MAX_RECIPE_BYTES = MAX_RECIPE_BYTES  # noqa: F811
    # Retention sweep bounds are delegated to the sibling retention module.
    MAX_PRUNE_DELETIONS = ExecutionLogRetention.MAX_PRUNE_DELETIONS
    MAX_WALK_DEPTH = ExecutionLogRetention.MAX_WALK_DEPTH
    MAX_PATH_SUFFIX_TRIES = 9

    # Process-wide cache for the current Windows account SID resolution
    # lives in the sibling writer module; the classmethod below keeps the
    # historical mock seam for capture-side hardening.

    @classmethod
    def default_root(cls) -> str:
        return ExecutionLogPaths.default_root()

    @classmethod
    def retention_days(cls) -> int:
        return ExecutionLogPaths.retention_days()

    @classmethod
    def _component(cls, value: object, field: str) -> str:
        return ExecutionLogPaths.component(value, field)

    @classmethod
    def job_log_path(
        cls,
        root: object,
        worker_id: str,
        runtime_kind: str,
        job_id: str,
        *,
        when: Optional[datetime] = None,
    ) -> Path:
        """Return the fixed host path of a job log without creating it."""
        return ExecutionLogPaths.job_log_path(root, worker_id, runtime_kind, job_id, when=when)

    @classmethod
    def redact_text(cls, value: object, secrets: Iterable) -> str:
        """Public one-shot redaction (delegates to the redactor component)."""
        return StreamRedactor.redact_text(value, secrets)

    @classmethod
    def open_writer(
        cls,
        root: object,
        worker_id: str,
        runtime_kind: str,
        job_id: str,
        *,
        secrets: Iterable = (),
        when: Optional[datetime] = None,
        maximum_bytes: Optional[int] = None,
        fsync_interval_bytes: Optional[int] = None,
    ) -> "ExecutionLogWriter":
        """Open a redaction-gated, bounded log writer under the fixed structure."""
        base = cls.job_log_path(root, worker_id, runtime_kind, job_id, when=when)
        options: Dict[str, object] = {"secrets": secrets}
        for name, value in (("maximum_bytes", maximum_bytes), ("fsync_interval_bytes", fsync_interval_bytes)):
            if value is not None:
                options[name] = value
        last_unavailable: Optional[ExecutionLogCaptureUnavailable] = None
        for index in range(cls.MAX_PATH_SUFFIX_TRIES):
            candidate = base if index == 0 else base.with_name(f"{base.stem}-{index}{base.suffix}")
            try:
                writer = ExecutionLogWriter(candidate, **options)
            except FileExistsError:
                continue
            except ExecutionLogCaptureUnavailable as exc:
                last_unavailable = exc
                break
            return writer
        raise last_unavailable or ExecutionLogCaptureUnavailable("execution log name could not be reserved")

    # Windows ACL hardening (the ACL regressions mock this hook): remove
    # inheritance and grant only the current user, SYSTEM and Administrators,
    # the same recipe as the native runtime secret materialization precedent.

    @classmethod
    def current_windows_user_sid(cls) -> str:
        """Resolve and cache the current Windows account SID for this process
        (writer-module implementation, classmethod for the historical
        capture mock seam)."""
        return current_windows_user_sid()

    @classmethod
    def _restrict_windows_acl(cls, path: Path) -> None:
        return restrict_windows_acl(path)

    # Redaction recipe creation/loading is delegated to the focused sibling
    # recipe module; the classmethods below keep the capture facade's public
    # callable API and the historical ``_restrict_windows_acl`` mock seam
    # (the ACL regressions mock it on this class).

    @classmethod
    def write_redaction_recipe(cls, path: object, secrets: Iterable) -> str:
        """Create a 0o600 one-shot redaction recipe and return its path."""
        return write_redaction_recipe(path, secrets, restrict_acl=cls._restrict_windows_acl)

    @classmethod
    def load_redaction_recipe(cls, path: object) -> List[str]:
        """Read and immediately delete a one-shot recipe file; corrupt input
        fails."""
        return load_redaction_recipe(path)

    # Detached capture child: keeps writing after the worker process dies.
    # Source runs re-invoke this module's script file (``--run-capture``);
    # frozen workers re-invoke the packaged executable with the sentinel.

    @classmethod
    def capture_child_command(
        cls,
        log_path: object,
        recipe_path: object,
        container_id: object = None,
        socket_path: object = None,
        stderr_only: bool = False,
        recipe_stream: bool = False,
    ) -> List[str]:
        """Command that runs the detached capture child on the same host.
        ``container_id`` selects the daemon streaming mode; ``socket_path``
        overrides the daemon socket (for isolated checks). ``recipe_stream``
        selects ephemeral framed-stdin secret transfer (no recipe file).
        Raises ValueError on an invalid container identifier."""
        if getattr(sys, "frozen", False):
            command = [sys.executable, CAPTURE_CHILD_CLI_SENTINEL]
        else:
            command = [sys.executable, os.path.abspath(__file__), "--run-capture"]
        command.extend(["--capture", os.fspath(Path(log_path))])
        if recipe_stream:
            command.extend(["--recipe-stream", "1"])
        else:
            command.extend(["--recipe", os.fspath(Path(recipe_path))])
        if container_id is not None:
            if not isinstance(container_id, str) or not DockerContainerLogSource.CONTAINER_ID_PATTERN.fullmatch(container_id):
                raise ValueError("invalid docker container identifier")
            command.extend(["--container", container_id])
        if socket_path is not None:
            command.extend(["--socket", os.fspath(Path(socket_path))])
        if stderr_only:
            command.extend(["--stderr-only", "1"])
        return command

    @classmethod
    def spawn_detached_writer(
        cls,
        log_path: object,
        recipe_path: object,
        stdin: object = None,
        recipe_stream: bool = False,
    ):
        """Start an orphan-safe capture child that outlives this process
        (``stdin`` is the read end of the executor output pipe; without it
        the child gets ``DEVNULL``). When ``recipe_stream`` is true the
        child reads a framed recipe from its stdin before output, so no
        recipe file is created."""
        return cls._spawn_detached(
            cls.capture_child_command(log_path, recipe_path, recipe_stream=recipe_stream), stdin
        )

    @classmethod
    def spawn_detached_docker_capture(
        cls,
        log_path: object,
        recipe_path: object,
        container_id: str,
        *,
        socket_path: object = None,
        stdin: object = None,
        stderr_only: bool = False,
        recipe_stream: bool = False,
    ):
        """Like :meth:`spawn_detached_writer`, but the child opens the daemon's
        container log follow stream itself, surviving the worker's death."""
        command = cls.capture_child_command(
            log_path,
            recipe_path,
            container_id=container_id,
            socket_path=socket_path,
            stderr_only=stderr_only,
            recipe_stream=recipe_stream,
        )
        return cls._spawn_detached(command, stdin)

    @classmethod
    def capture_relay_command(cls, log_path: object, recipe_path: object, executor_command: object, *, stderr_only: bool = False, recipe_stream: bool = False) -> List[str]:
        """Command that runs the executor through the executor-owning relay
        child (S5, native): the relay owns the executor's own stdout/stderr
        pipes, forwards every byte line verbatim to its own stdout/stderr
        (the worker's live stream) and feeds the detached redaction-gated
        capture child, so capture continues after the worker or its live
        pipe dies, and relay broken pipes or capture loss never fail, kill
        or stall the backup. Executor exit codes propagate to the relay's
        exit status."""
        if (
            not isinstance(executor_command, (list, tuple))
            or not executor_command
            or not all(isinstance(item, str) for item in executor_command)
        ):
            raise ValueError("executor runtime command must be a non-empty string argv list")
        fixed = ["--run-relay", "--capture", os.fspath(Path(log_path))]
        if recipe_stream:
            fixed.extend(["--recipe-stream", "1"])
        else:
            fixed.extend(["--recipe", os.fspath(Path(recipe_path))])
        if stderr_only:
            fixed.extend(["--stderr-only", "1"])
        fixed.append("--")
        if getattr(sys, "frozen", False):
            command = [sys.executable, CAPTURE_CHILD_CLI_SENTINEL]
        else:
            command = [sys.executable, os.path.abspath(__file__)]
        return [*command, *fixed, *executor_command]

    @staticmethod
    def _spawn_detached(command: List[str], stdin: object = None):
        """Launch a detached child that survives its spawner (POSIX: its own
        session; Windows: detached with a new process group)."""
        flags: Dict[str, object] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            flags["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            flags["start_new_session"] = True
        try:
            return subprocess.Popen(
                command,
                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                **flags,
            )
        except OSError as exc:
            raise ExecutionLogCaptureUnavailable("detached execution log capture could not start") from exc

    # S3: retention. The 30-day managed-log pruning sweep lives in the
    # focused sibling retention module; this facade keeps the historical
    # seam and never raises: callers report the summary (S4).

    @classmethod
    def prune(
        cls,
        root: object = None,
        *,
        now: Optional[datetime] = None,
        retention_days: Optional[int] = None,
    ) -> Dict[str, object]:
        """Delete managed logs older than the retention window; never raises."""
        return ExecutionLogRetention.prune(root, now=now, retention_days=retention_days)


class DockerContainerLogSource:
    """Read-only container log follow stream through the project's Docker SDK.

    Uses exactly the logs API ``DockerRuntimeAdapter`` relies on
    (``container.logs(stdout=True, stderr=True, stream=True, follow=True)``);
    the two read-only SDK calls and the pre-validated container identifier
    cannot modify a container. ``client_factory`` is the SDK test seam.
    """

    # First character alphanumeric, then only alphanumerics plus ``._-``,
    # matching Docker's container-name grammar and id form.
    CONTAINER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    DEFAULT_UNIX_SOCKET = "/var/run/docker.sock"
    DEFAULT_WINDOWS_PIPE = r"\\.\pipe\docker_engine"
    # A named pipe path is translated into the exact URL form docker-py's own
    # parser normalizes (``npipe:////./pipe/<name>``); see socket_base_url.
    _WINDOWS_PIPE_PREFIX = "//./pipe/"
    _PIPE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

    def __init__(
        self,
        container_id: str,
        *,
        socket_path: Optional[str] = None,
        client_factory=None,
        stderr_only: bool = False,
    ):
        if not isinstance(container_id, str) or not self.CONTAINER_ID_PATTERN.fullmatch(container_id):
            raise ValueError("invalid docker container identifier")
        self._container_id = container_id
        self._socket_path = socket_path or self.default_socket()
        self._client_factory = client_factory
        self._stderr_only = stderr_only

    @property
    def container_id(self) -> str:
        return self._container_id

    @classmethod
    def default_socket(cls) -> str:
        return cls.DEFAULT_WINDOWS_PIPE if os.name == "nt" else cls.DEFAULT_UNIX_SOCKET

    @classmethod
    def socket_base_url(cls, socket_path: str) -> str:
        """Translate a socket/pipe path into the SDK base URL form.

        Exactly two forms are supported: a Windows named-pipe path
        (``\\.\pipe\<name>`` or ``//./pipe/<name>``), translated to
        ``npipe:////./pipe/<name>`` (the ``npipe://\\.\pipe\...`` form
        collapses to ``npipe:`` in ``parse_host`` and never reaches a
        daemon), and a Unix socket translated to ``unix://<path>``; anything
        else raises :class:`ValueError` before daemon contact.
        """
        if not isinstance(socket_path, str):
            raise ValueError("invalid docker daemon socket location")
        normalized = socket_path.replace("\\", "/").strip()
        if normalized.startswith(cls._WINDOWS_PIPE_PREFIX):
            pipe_name = normalized[len(cls._WINDOWS_PIPE_PREFIX):].strip("/")
            if not cls._PIPE_NAME_PATTERN.fullmatch(pipe_name):
                raise ValueError("invalid docker daemon named pipe path")
            return f"npipe:////./pipe/{pipe_name}"
        if normalized.startswith("/"):
            return f"unix://{normalized}"
        raise ValueError("unsupported docker daemon socket location")

    @staticmethod
    def _real_client(socket_path: str):
        if docker is None:
            raise ExecutionLogCaptureUnavailable("docker SDK is unavailable")
        try:
            return docker.DockerClient(base_url=DockerContainerLogSource.socket_base_url(socket_path))
        except Exception as exc:
            raise ExecutionLogCaptureUnavailable("docker daemon client could not be created") from exc

    @staticmethod
    def _closed(stream) -> Iterator[bytes]:
        """Yield SDK payloads once and always close the SDK stream afterwards."""
        try:
            yield from stream
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def open_stream(self) -> Iterator[bytes]:
        """Open the follow stream, validating the daemon answer first
        (raises :class:`ExecutionLogCaptureUnavailable` when the daemon is
        unreachable or the read-only logs request is rejected)."""
        factory = self._client_factory or self._real_client
        try:
            client = factory(self._socket_path)
            container = client.containers.get(self._container_id)
            stream = container.logs(stdout=not self._stderr_only, stderr=True, stream=True, follow=True)
        except ExecutionLogCaptureUnavailable:
            raise
        except Exception as exc:
            raise ExecutionLogCaptureUnavailable("docker daemon log stream could not be opened") from exc
        return self._closed(stream)


class _RelayCaptureSink:
    """Bounded, fail-open capture-side sink inside the relay child.

    The relay owns the executor, so a slow capture child can never be
    allowed to stall the executor or the live stream: every line is offered
    through a small bounded queue, the backlog is dropped instead of
    blocking (counted), a lost/broken capture side is permanently disabled,
    and ``finalize`` hands the child EOF once so it can persist the redacted
    evidence and complete on its own."""

    _EOF = object()
    _DRAIN_SECONDS = RELAY_CAPTURE_DRAIN_SECONDS

    def __init__(self, stream: object):
        self._stream = stream
        self._queue: "queue.Queue" = queue.Queue(maxsize=RELAY_SINK_QUEUE_DEPTH)
        self._broken = False
        self._closed = False
        self._drops = 0
        self._thread = threading.Thread(target=self._pump, name="execution-log-relay-sink", daemon=True)
        self._thread.start()

    @property
    def broken(self) -> bool:
        return self._broken

    @property
    def closed(self) -> bool:
        """The capture stream was handed EOF (or failed) and is closed."""
        return self._closed

    @property
    def dropped(self) -> int:
        return self._drops

    def write(self, chunk: bytes) -> None:
        if self._broken or self._closed or not isinstance(chunk, (bytes, bytearray)):
            return
        try:
            self._queue.put_nowait(bytes(chunk))
        except queue.Full:
            self._drops += 1

    def finalize(self) -> None:
        """Signal EOF and wait (bounded) for the backlog to be handed over."""
        if self._closed:
            return
        try:
            self._queue.put(self._EOF, timeout=self._DRAIN_SECONDS)
        except queue.Full:
            self._close_stream_async()
            return
        self._thread.join(self._DRAIN_SECONDS)
        if not self._closed:
            # A buffered close may wait on the pump's blocked write lock.
            # Never perform that potentially blocking close on this thread.
            self._close_stream_async()

    def abandon(self) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(self._EOF)
        except queue.Full:
            self._close_stream_async()

    def _close_stream_async(self) -> None:
        self._broken = True
        threading.Thread(target=self._close_stream, name="execution-log-relay-close", daemon=True).start()

    def _pump(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._EOF:
                break
            if self._broken:
                continue
            try:
                self._stream.write(item)
                self._stream.flush()
            except Exception:
                # The capture child is gone; disable the capture side and
                # drain leftovers so nothing blocks the relay threads.
                self._broken = True
                try:
                    while self._queue.get_nowait() is not self._EOF:
                        pass
                except queue.Empty:
                    pass
                break
        self._close_stream()

    def _close_stream(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._stream, "close", None)
            if callable(close):
                close()
        except Exception:
            pass


def _relay_live_sink(text_stream: object):
    """Unbuffered byte sink for live forwarding inside the relay.

    Buffered writers keep dirty data across a broken-pipe failure and make
    interpreter exit fail (Windows exit status 120), and even a healthy run
    would lose byte-level line framing; a raw sink keeps the live stream a
    byte-verbatim copy of the executor output (the worker still applies its
    own universal-newline reading and redaction, exactly as before)."""
    if text_stream is None:
        return None
    raw = getattr(text_stream, "raw", None)
    if raw is not None:
        return raw
    buffer = getattr(text_stream, "buffer", None)
    if buffer is not None:
        raw = getattr(buffer, "raw", None)
        return raw if raw is not None else buffer
    return text_stream


def _relay_forward_source(source, live_writer, stream_flags: List[bool], sink: "Optional[_RelayCaptureSink]") -> None:
    """Forward one executor stream, byte line by byte line, to the live
    stream and the capture sink. The executor output itself is never
    touched: live failures only disable the live side, capture failures only
    disable the capture side."""
    while True:
        try:
            line = source.readline(READ_CHUNK_BYTES)
        except Exception:
            break
        if not line:
            break
        if stream_flags[0]:
            try:
                live_writer.write(line)
                live_writer.flush()
            except Exception:
                # The live consumer (worker) is gone or wedged: keep the
                # executor running and keep capturing.
                stream_flags[0] = False
        if sink is not None:
            sink.write(line)
    try:
        source.close()
    except Exception:
        pass


def _install_relay_stop_handlers(executor_holder: List[object]) -> None:
    """Cancellation/timeout stays fast and tree-correct: the relay is asked
    to stop first (POSIX SIGTERM, Windows CTRL_BREAK to its new process
    group) and terminates its executor itself. The detached capture child
    (its own session/process group) survives and finalizes the evidence."""

    def stop(signum, frame):
        executor = executor_holder[0]
        if executor is not None:
            try:
                executor.terminate()
            except Exception:
                pass
        os._exit(130)

    try:
        if os.name == "nt":
            signal.signal(signal.CTRL_BREAK_EVENT, stop)
        else:
            signal.signal(signal.SIGTERM, stop)
    except Exception:
        pass


def _run_relay_child(log_path: object, recipe_path: object, executor_argv: List[str], *, stderr_only: bool = False, recipe_stream: bool = False) -> int:
    """Executor-owning relay child body (S5, native).

    Spawns the detached capture child (recipes/paths only, no secrets in
    argv), then the real executor with the worker-built environment, and
    forwards output byte lines verbatim to the worker's live streams and to
    the redaction-gated capture child. Any failure on either side disables
    only that side; the executor runs untouched and its exit code becomes
    the relay's exit code, preserving the job's result semantics.

    When ``recipe_stream`` is true the relay reads a framed recipe from its
    own stdin (the parent wrote it) and forwards it to the writer's stdin
    before executor output, so no recipe file is ever created on disk."""
    executor_holder: List[object] = [None]
    _install_relay_stop_handlers(executor_holder)
    child = None
    if recipe_stream:
        try:
            recipe_secrets = read_recipe_frame(sys.stdin.buffer)
        except RecipeFrameError:
            # Fail-open: a corrupt frame skips capture but never prevents
            # the executor from running.
            recipe_secrets = None
        if recipe_secrets is not None:
            try:
                child = ExecutionLogCapture.spawn_detached_writer(
                    log_path, None, stdin=subprocess.PIPE, recipe_stream=True
                )
            except (ExecutionLogCaptureUnavailable, OSError, ValueError):
                child = None
            if child is not None:
                try:
                    child.stdin.write(encode_recipe_frame(recipe_secrets))
                    child.stdin.flush()
                except (OSError, BrokenPipeError):
                    try:
                        child.stdin.close()
                    except Exception:
                        pass
                    child = None
        else:
            child = None
    else:
        try:
            child = ExecutionLogCapture.spawn_detached_writer(log_path, recipe_path, stdin=subprocess.PIPE)
        except (ExecutionLogCaptureUnavailable, OSError, ValueError):
            child = None
    sink = None
    capture_stream = getattr(child, "stdin", None) if child is not None else None
    if capture_stream is not None:
        sink = _RelayCaptureSink(capture_stream)
    try:
        executor = subprocess.Popen(list(executor_argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception:
        if sink is not None:
            sink.abandon()
        try:
            sys.stderr.write("worker execution log relay could not start the runtime command\n")
            sys.stderr.flush()
        except Exception:
            pass
        return 127
    executor_holder[0] = executor
    live_out = _relay_live_sink(sys.stdout)
    live_err = _relay_live_sink(sys.stderr)
    stdout_flags: List[bool] = [True]
    stderr_flags: List[bool] = [True]
    threads = [
        threading.Thread(target=_relay_forward_source, args=(executor.stdout, live_out, stdout_flags, None if stderr_only else sink), daemon=True),
        threading.Thread(target=_relay_forward_source, args=(executor.stderr, live_err, stderr_flags, sink), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        code = executor.wait()
    except Exception:
        code = executor.returncode if isinstance(executor.returncode, int) else 1
    if sink is not None:
        sink.finalize()
    return code if isinstance(code, int) else 1


def _load_child_recipe(recipe_path: object):
    """Recipe step of both child modes (integrity gates persistence):
    returns ``(None, 2)`` for a missing or corrupt recipe; reading deletes
    the one-shot recipe file."""
    try:
        return ExecutionLogCapture.load_redaction_recipe(recipe_path), None
    except ExecutionLogCaptureUnavailable:
        return None, CAPTURE_CHILD_EXIT_RECIPE_FAILURE


def _open_child_writer(log_path: object, secrets):
    """Writer step of both child modes; an unusable log location returns
    ``(None, 3)``."""
    try:
        return ExecutionLogWriter(log_path, secrets=secrets), 0
    except (ExecutionLogCaptureUnavailable, ValueError, FileExistsError):
        return None, CAPTURE_CHILD_EXIT_UNAVAILABLE


def _capture_docker_log_stream(
    log_path: object,
    recipe_path: object,
    container_id: str,
    *,
    socket_path: Optional[str] = None,
    docker_source: Optional["DockerContainerLogSource"] = None,
    stderr_only: bool = False,
    recipe_stream: bool = False,
) -> int:
    """Detached capture child body for the Docker daemon streaming mode.

    Fail-closed ordering: identifier and recipe are validated (and the recipe
    deleted) before the daemon is contacted, and the daemon stream is opened
    before the log file, so an unreachable or rejecting daemon persists
    nothing and returns 4. A stream breaking after the log is open keeps the
    redacted partial evidence, records an ``interrupted`` event and returns 1.
    When ``recipe_stream`` is true the recipe is read as a framed stdin
    transfer instead of from a file."""
    try:
        source = docker_source if docker_source is not None else DockerContainerLogSource(
            container_id, socket_path=socket_path, stderr_only=stderr_only
        )
    except (ValueError, TypeError):
        return CAPTURE_CHILD_EXIT_RECIPE_FAILURE
    if recipe_stream:
        try:
            secrets = read_recipe_frame(sys.stdin.buffer)
        except RecipeFrameError:
            return CAPTURE_CHILD_EXIT_RECIPE_FAILURE
    else:
        secrets, _ = _load_child_recipe(recipe_path)
        if secrets is None:
            return CAPTURE_CHILD_EXIT_RECIPE_FAILURE
    try:
        stream = source.open_stream()
    except (ExecutionLogCaptureUnavailable, OSError, ValueError):
        return CAPTURE_CHILD_EXIT_DAEMON_FAILURE
    writer, exit_code = _open_child_writer(log_path, secrets)
    if writer is None:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
        return exit_code
    try:
        writer.write_event(
            "attached", f"pid={os.getpid()} container={getattr(source, 'container_id', container_id)}"
        )
        try:
            for chunk in stream:
                writer.write_chunk(chunk)
            return 0
        except Exception:
            writer.mark_interrupted("docker daemon log stream failed before EOF")
            return 1
    finally:
        writer.close()


def _parse_child_options(argv: List[str]) -> Optional[Dict[str, object]]:
    """Parse fixed child options plus the executor-owning relay separator:
    ``--`` ends the option block and carries the executor argv verbatim. A
    repeated, malformed or unknown argument parses as nothing (malformed
    arguments persist nothing)."""
    options: Dict[str, object] = {
        "--capture": None,
        "--recipe": None,
        "--recipe-stream": None,
        "--container": None,
        "--socket": None,
        "--stderr-only": None,
        "--": None,
    }
    index = 0
    while index < len(argv):
        name = argv[index]
        if name == "--":
            rest = list(argv[index + 1 :])
            if options["--"] is not None or any(not isinstance(item, str) for item in rest):
                return None
            options["--"] = rest
            break
        if name not in options or index + 1 >= len(argv) or options[name] is not None:
            return None
        options[name] = argv[index + 1]
        index += 2
    return options


def run_capture_child(argv: List[str], *, docker_source: Optional["DockerContainerLogSource"] = None) -> int:
    """Detached capture child entry: stdin (or ``--container`` daemon stream)
    -> bounded redacted log file; the ``--run-relay`` sentinel selects the
    executor-owning relay mode (the executor argv follows ``--``).
    Statuses 2 persist nothing, 1 keeps the redacted partial evidence, 0
    completes. A live child keeps reading to EOF even when the worker that
    spawned it has already exited."""
    arguments = list(argv)
    relay_mode = bool(arguments) and arguments[0] == "--run-relay"
    if relay_mode:
        arguments = arguments[1:]
    options = _parse_child_options(arguments)
    log_path = options["--capture"] if options else None
    recipe_path = options["--recipe"] if options else None
    recipe_stream = options["--recipe-stream"] if options else None
    if not log_path or recipe_stream not in (None, "1"):
        return CAPTURE_CHILD_EXIT_RECIPE_FAILURE
    if (recipe_path is None) == (recipe_stream is None) or recipe_path == "":
        return CAPTURE_CHILD_EXIT_RECIPE_FAILURE
    if options["--stderr-only"] not in (None, "1"):
        return CAPTURE_CHILD_EXIT_RECIPE_FAILURE
    stderr_only = options["--stderr-only"] == "1"
    executor_argv = options["--"]
    if relay_mode or executor_argv is not None:
        if executor_argv is None or not executor_argv:
            return CAPTURE_CHILD_EXIT_RECIPE_FAILURE
        return _run_relay_child(log_path, recipe_path, list(executor_argv), stderr_only=stderr_only, recipe_stream=bool(recipe_stream))
    container_id = options["--container"]
    if container_id is None:
        # A direct stdin stream carries no source identifier; accepting this
        # option would falsely promise that binary stdout cannot be logged.
        if stderr_only:
            return CAPTURE_CHILD_EXIT_RECIPE_FAILURE
        if recipe_stream:
            try:
                secrets = read_recipe_frame(sys.stdin.buffer)
            except RecipeFrameError:
                return CAPTURE_CHILD_EXIT_RECIPE_FAILURE
        else:
            secrets, _ = _load_child_recipe(recipe_path)
            if secrets is None:
                return CAPTURE_CHILD_EXIT_RECIPE_FAILURE
        writer, exit_code = _open_child_writer(log_path, secrets)
        if writer is None:
            return exit_code
        try:
            try:
                writer.write_event("attached", f"pid={os.getpid()}")
                source = sys.stdin.buffer
                read_chunk = getattr(source, "read1", source.read)
                while True:
                    chunk = read_chunk(READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    writer.write_chunk(chunk)
                return 0
            except OSError:
                return 1
        finally:
            writer.close()
    if not container_id:
        return CAPTURE_CHILD_EXIT_RECIPE_FAILURE
    return _capture_docker_log_stream(
        log_path,
        recipe_path,
        container_id,
        socket_path=options["--socket"],
        docker_source=docker_source,
        stderr_only=stderr_only,
        recipe_stream=bool(recipe_stream),
    )


def main(argv: Optional[List[str]] = None) -> int:
    """Module-script entry: ``<script> --run-capture <child options>`` or,
    for the executor-owning native relay, ``<script> --run-relay ...``."""
    arguments = list(sys.argv[1:]) if argv is None else list(argv)
    if arguments and arguments[0] == "--run-capture":
        return run_capture_child(arguments[1:])
    if arguments and arguments[0] == "--run-relay":
        return run_capture_child(arguments)
    return CAPTURE_CHILD_EXIT_RECIPE_FAILURE


__all__ = [
    "CAPTURE_CHILD_CLI_SENTINEL",
    "CAPTURE_CHILD_EXIT_DAEMON_FAILURE",
    "DockerContainerLogSource",
    "ExecutionLogCapture",
    "ExecutionLogCaptureUnavailable",
    "ExecutionLogWriter",
    "StreamRedactor",
    "run_capture_child",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
