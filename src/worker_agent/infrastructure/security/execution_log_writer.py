"""Focused execution-log file writer component (S5, S6 in
``odd/tasks/persistent-worker-execution-logs.md``).

Contains the secure, bounded, crash-resistant log-file writer and the
Windows ACL hardening it shares with the capture module's redaction-recipe
creation; no structure, spawn, retention or Docker logic lives here, so the
sibling ``execution_log_capture`` module stays the single capture entrypoint
(script and frozen dispatch, Docker and stdin modes) and this slice stays a
small, self-contained module that imports nothing from the capture module,
the path policy or the Docker SDK.

Security and crash invariants (unchanged): nothing reaches disk before
redaction - the chunk-boundary split gate lives in the focused sibling
module :mod:`execution_log_redaction` (:class:`StreamRedactor`), and event
detail text is redacted whole (secrets and repository URL patterns) before
the 512-character recordable-detail cut, never sliced mid-secret; the file
is created exclusively (``O_EXCL``, ``0o600``) so a colliding job log raises
``FileExistsError`` untouched, and the directory is prepared at ``0o700``;
on Windows the empty file's DACL is hardened (inheritance removed, current
user plus SYSTEM and Administrators only) before any secret byte, failing
closed by deleting the created file; output is bounded by
``maximum_bytes``, records a ``truncated`` event once and suppresses
further writes; the file is fsynced at least once per
``fsync_interval_bytes`` and always before the final close.
"""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    from .execution_log_redaction import StreamRedactor
except ImportError:  # source-mode child ran as a directly executed script:
    # containing directory sys.path[0] keeps the sibling import working
    # from any cwd.
    from execution_log_redaction import StreamRedactor  # type: ignore[no-redef]

MAX_LOG_BYTES = 64 * 1024 * 1024
FSYNC_INTERVAL_BYTES = 256 * 1024

_SID_PATTERN_START = "S-"
_ICACLS_GRANTS = ("*S-1-5-18:F", "*S-1-5-32-544:F")

# Process-wide cache for the current Windows account SID resolution.
_WINDOWS_SID = ""


class ExecutionLogCaptureUnavailable(RuntimeError):
    """Raised when execution output cannot be persisted on the host."""


def current_windows_user_sid() -> str:
    """Resolve and cache the current Windows account SID for this process."""
    global _WINDOWS_SID
    if _WINDOWS_SID:
        return _WINDOWS_SID
    try:
        completed = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExecutionLogCaptureUnavailable("windows account SID could not be resolved") from exc
    row = next(iter(csv.reader(io.StringIO(completed.stdout))), None)
    sid = row[-1].strip() if row else ""
    if not sid.startswith(_SID_PATTERN_START):
        raise ExecutionLogCaptureUnavailable("windows account SID could not be resolved")
    _WINDOWS_SID = sid
    return sid


def restrict_windows_acl(path: Path) -> None:
    """Remove inheritance and grant only the current user, SYSTEM and
    Administrators (the same recipe as the native runtime secret
    materialization precedent). Raises the unavailable error on failure."""
    try:
        sid = current_windows_user_sid()
        completed = subprocess.run(
            ["icacls", str(path),
             "/inheritance:r", "/grant:r", f"*{sid}:F",
             "/grant:r", *_ICACLS_GRANTS],
            capture_output=True,
        )
    except OSError as exc:
        raise ExecutionLogCaptureUnavailable(f"windows ACL hardening failed for {path}") from exc
    if completed.returncode != 0:
        raise ExecutionLogCaptureUnavailable(f"windows ACL hardening failed for {path}")


class ExecutionLogWriter:
    """Append-only, redaction-gated, bounded execution log file: exclusive
    0o600 creation, fsync at close and at least once per
    ``fsync_interval_bytes``; chunk-boundary split safety (secrets and URLs
    held until whole, tokens dropped fail-closed at the memory bound) lives
    in :class:`StreamRedactor`; the file never exceeds ``maximum_bytes``."""

    EVENT_HEADROOM_BYTES = 256
    MINIMUM_BYTES = 512
    # Test-pinned alias of the redactor's bounded-memory limit.
    MAX_PENDING_CHARS = StreamRedactor.MAX_PENDING_CHARS

    def __init__(
        self,
        filename: object,
        *,
        secrets: Iterable = (),
        maximum_bytes: int = MAX_LOG_BYTES,
        fsync_interval_bytes: int = FSYNC_INTERVAL_BYTES,
    ):
        if isinstance(maximum_bytes, bool) or not isinstance(maximum_bytes, int) or maximum_bytes <= 0:
            raise ValueError("execution log maximum_bytes must be a positive integer")
        if maximum_bytes > MAX_LOG_BYTES:
            raise ValueError("execution log maximum_bytes exceeds the permitted bound")
        if maximum_bytes < self.MINIMUM_BYTES:
            raise ValueError("execution log maximum_bytes leaves no usable budget")
        if (
            isinstance(fsync_interval_bytes, bool)
            or not isinstance(fsync_interval_bytes, int)
            or fsync_interval_bytes < 0
        ):
            raise ValueError("execution log fsync_interval_bytes must be a non-negative integer")
        self._path = Path(filename)
        self._maximum_bytes = maximum_bytes
        self._fsync_interval_bytes = fsync_interval_bytes
        self._redactor = StreamRedactor(secrets)
        self._lock = threading.Lock()
        self._emitted = 0
        self._fsynced_at = 0
        self._truncated = False
        self._descriptor = -1
        self._open_file()
        self.write_event("started", f"pid={os.getpid()}")

    @classmethod
    def _maximum_bytes_limit(cls) -> int:
        """Validation boundary for callers that compose writer bounds."""
        return MAX_LOG_BYTES

    @property
    def truncated(self) -> bool:
        return self._truncated

    # Context manager support so callers cannot leak the descriptor.

    def __enter__(self) -> "ExecutionLogWriter":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.close()
        return False

    def _open_file(self) -> None:
        parent = self._path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ExecutionLogCaptureUnavailable(f"execution log directory is unavailable: {parent}") from exc
        if os.name != "nt":
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        try:
            self._descriptor = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise
        except OSError as exc:
            raise ExecutionLogCaptureUnavailable(f"execution log file is unavailable: {self._path}") from exc
        if os.name == "nt":
            try:
                # The log file's inherited Windows DACL is user-readable:
                # ACL before any content, fail closed (unrestricted, no log).
                restrict_windows_acl(self._path)
            except ExecutionLogCaptureUnavailable:
                self._discard_locked()
                raise
        else:
            try:
                os.fchmod(self._descriptor, 0o600)
            except OSError:
                pass

    def _discard_locked(self) -> None:
        """Abandon an exclusively created but unusable log file."""
        try:
            os.close(self._descriptor)
        except OSError:
            pass
        self._descriptor = -1
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def write_chunk(self, raw: object) -> None:
        """Append one executor output chunk after redaction and bounding."""
        with self._lock:
            if self._truncated or self._descriptor < 0:
                return
            emitted, overflowed = self._redactor.feed(raw)
            if emitted:
                self._append_bounded_locked(
                    self._redactor.redact_text(emitted, self._redactor.secrets).encode("utf-8")
                )
            if overflowed:
                self._append_event_locked(
                    "truncated",
                    f"unterminated token beyond {self.MAX_PENDING_CHARS} characters dropped",
                )

    def write_event(self, kind: str, detail: str = "") -> None:
        """Append a small structured, redacted marker line."""
        with self._lock:
            if self._descriptor < 0:
                return
            self._append_event_locked(kind, detail)

    def mark_interrupted(self, detail: str = "") -> None:
        self.write_event("interrupted", detail)

    def close(self) -> None:
        with self._lock:
            if self._descriptor < 0:
                return
            descriptor = self._descriptor
            try:
                if not self._truncated:
                    self._append_bounded_locked(
                        self._redactor.redact_text(self._redactor.flush(), self._redactor.secrets).encode("utf-8")
                    )
                    if not self._truncated:
                        self._append_event_locked("completed", "")
                try:
                    os.fsync(descriptor)
                except OSError:
                    pass
            finally:
                self._descriptor = -1
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    # Internal append path: caller must hold the lock.

    def _append_bounded_locked(self, encoded: bytes) -> None:
        budget = self._maximum_bytes - self.EVENT_HEADROOM_BYTES - self._emitted
        if budget <= 0:
            self._flag_truncated_locked()
            return
        if len(encoded) > budget:
            bounded = encoded[:budget].decode("utf-8", errors="ignore").encode("utf-8")
            if bounded:
                self._append_bytes_locked(bounded)
                self._emitted += len(bounded)
                self._maybe_fsync_locked(tolerate_failure=True)
            self._flag_truncated_locked()
            return
        self._append_bytes_locked(encoded)
        self._emitted += len(encoded)
        self._maybe_fsync_locked(tolerate_failure=False)

    def _maybe_fsync_locked(self, *, tolerate_failure: bool) -> None:
        if not self._fsync_interval_bytes or self._emitted - self._fsynced_at < self._fsync_interval_bytes:
            return
        try:
            os.fsync(self._descriptor)
            self._fsynced_at = self._emitted
        except OSError:
            if tolerate_failure:
                # Past the truncation boundary: avoid re-attempting fsync.
                self._fsynced_at = self._emitted

    def _flag_truncated_locked(self) -> None:
        if self._truncated:
            return
        self._truncated = True
        self._redactor.discard()
        self._append_event_locked("truncated", f"output exceeded {self._maximum_bytes} bytes")

    def _append_event_locked(self, kind: str, detail: str = "") -> None:
        safe_kind = kind if isinstance(kind, str) and re.fullmatch(r"[a-z0-9_.-]{1,32}", kind) else "event"
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        # Redact the COMPLETE detail (secrets and repository URL patterns)
        # before the recordable-detail cut: slicing first could cut a secret
        # across the 512-character boundary and partially persist it. The cut
        # is at code-point level, so the following UTF-8 encoding never splits
        # a multi-byte character.
        redacted_detail = self._redactor.redact_text(detail or "", self._redactor.secrets)[:512]
        encoded = f"[{stamp}] [{safe_kind}] {redacted_detail}\n".encode("utf-8")
        budget = self._maximum_bytes - self._emitted
        if budget <= 0 or len(encoded) > budget:
            return
        self._append_bytes_locked(encoded)
        self._emitted += len(encoded)

    def _append_bytes_locked(self, encoded: bytes) -> None:
        view = memoryview(encoded)
        while view:
            view = view[os.write(self._descriptor, view):]


__all__ = [
    "ExecutionLogCaptureUnavailable",
    "ExecutionLogWriter",
    "current_windows_user_sid",
    "restrict_windows_acl",
]