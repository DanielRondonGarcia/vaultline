"""Focused one-shot redaction-recipe component (T1 S6,
``odd/tasks/persistent-worker-execution-logs.md``).

A redaction recipe is the only channel that lets the detached capture child
redact executor output without the worker process being alive: the worker
serializes the job's secret values into a JSON file, and the child reads it
once and deletes it. Only paths and fixed options travel on argv; secret
values travel here, never on the command line.

Security and crash invariants (unchanged from the capture prototype this
module was extracted from): owner-only creation precedes any secret byte
(Windows: the EMPTY file's DACL is hardened first, inheritance removed and
the current user plus SYSTEM and Administrators only, via the committed
:mod:`execution_log_writer` ACL primitives; POSIX: ``O_EXCL`` ``0o600`` and a
parent directory at ``0o700``); a failure unlinks the created file; a
colliding destination raises ``FileExistsError`` untouched; recipes are
bounded (item count and byte size) and rejected with ``ValueError`` before
anything touches disk. Loading issues bounded read requests of at most
``MAX_RECIPE_BYTES + 1`` bytes, validates and immediately deletes the recipe,
and rejects any file bigger than ``MAX_RECIPE_BYTES`` before JSON decoding,
so a corrupt, missing, non-list, over-long or oversized recipe fails closed (:class:`ExecutionLogCaptureUnavailable`) and no capture child ever
persists raw output. The module imports nothing from the capture, path
policy, retention or Docker modules and stays directly-executable-script
importable (source-mode child runs it from any cwd).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Callable, Iterable, List, Optional

try:
    from .execution_log_writer import (
        ExecutionLogCaptureUnavailable,
        restrict_windows_acl,
    )
except ImportError:  # source-mode child ran as a directly executed script:
    # containing directory sys.path[0] keeps the sibling import working
    # from any cwd.
    from execution_log_writer import (  # type: ignore[no-redef]
        ExecutionLogCaptureUnavailable,
        restrict_windows_acl,
    )

MAX_RECIPE_ITEMS = 512
MAX_RECIPE_BYTES = 16 * 1024 * 1024


def _normalized_secret_values(secrets: Iterable) -> List[str]:
    """De-duplicated, non-empty string secrets in first-seen order."""
    return list(dict.fromkeys(secret for secret in secrets if isinstance(secret, str) and secret))


def write_redaction_recipe(path: object, secrets: Iterable, *, restrict_acl: Optional[Callable[[Path], None]] = None) -> str:
    """Create a 0o600 one-shot redaction recipe and return its path. Only
    non-empty string secrets are kept and de-duplicated; bounds are enforced
    before any filesystem work. Fail-closed: owner-only creation (Windows: ACL
    on the EMPTY file) precedes any secret byte; a failure unlinks; a
    colliding destination raises ``FileExistsError``, untouched."""
    values = _normalized_secret_values(secrets)
    if len(values) > MAX_RECIPE_ITEMS:
        raise ValueError("redaction recipe exceeds the permitted item count")
    encoded = json.dumps(values, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_RECIPE_BYTES:
        raise ValueError("redaction recipe exceeds the permitted size")
    harden = restrict_acl if restrict_acl is not None else restrict_windows_acl
    target = Path(path)
    parent = target.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExecutionLogCaptureUnavailable(
            f"redaction recipe directory could not be prepared: {parent}"
        ) from exc
    if os.name != "nt":
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise  # A preexisting colliding destination surfaces unchanged.
    # True while the descriptor is not yet owned by fdopen, so a failure
    # below can close it exactly once.
    unowned_descriptor = True
    try:
        if os.name == "nt":
            # Harden the EMPTY file first: no secret byte may reach a
            # file whose DACL is still the inherited user-readable one.
            harden(target)
        with os.fdopen(descriptor, "wb") as handle:
            unowned_descriptor = False
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            if os.name != "nt":
                try:
                    os.fchmod(handle.fileno(), 0o600)
                except OSError:
                    pass
    except (OSError, ExecutionLogCaptureUnavailable) as exc:
        if unowned_descriptor:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(target)
        except OSError:
            pass
        if isinstance(exc, ExecutionLogCaptureUnavailable):
            raise
        raise ExecutionLogCaptureUnavailable(f"redaction recipe could not be written: {target}") from exc
    return str(target)


def load_redaction_recipe(path: object) -> List[str]:
    """Read and immediately delete a one-shot recipe file; corrupt, missing,
    non-list, over-long, oversized or truncated-beyond-the-bound input fails
    closed. The read itself is bounded: at most ``MAX_RECIPE_BYTES + 1`` bytes
    are ever requested from the file, and any file that yields more bytes than
    ``MAX_RECIPE_BYTES`` — including an oversized file whose leading bytes form
    a valid JSON value padded only with whitespace — is rejected before any
    JSON decoding."""
    target = Path(path)
    try:
        chunks: List[bytes] = []
        pending = MAX_RECIPE_BYTES + 1
        # A blocking open of a FIFO waits indefinitely for a writer before we
        # can inspect it. Open nonblocking on POSIX, then validate the opened
        # descriptor rather than a path which could be replaced between calls.
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ExecutionLogCaptureUnavailable("redaction recipe is not a regular file")
            while pending > 0:
                chunk = os.read(descriptor, pending)
                if not chunk:
                    break
                chunks.append(chunk)
                pending -= len(chunk)
        finally:
            os.close(descriptor)
        raw = b"".join(chunks)
    except OSError as exc:
        raise ExecutionLogCaptureUnavailable("redaction recipe could not be read") from exc
    finally:
        try:
            os.unlink(target)
        except OSError:
            pass
    if len(raw) > MAX_RECIPE_BYTES:
        raise ExecutionLogCaptureUnavailable("redaction recipe exceeds the permitted size")
    try:
        values = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ExecutionLogCaptureUnavailable("redaction recipe was corrupt") from exc
    if not isinstance(values, list) or len(values) > MAX_RECIPE_ITEMS:
        raise ExecutionLogCaptureUnavailable("redaction recipe was invalid")
    return _normalized_secret_values(values)


__all__ = [
    "MAX_RECIPE_BYTES",
    "MAX_RECIPE_ITEMS",
    "ExecutionLogCaptureUnavailable",
    "load_redaction_recipe",
    "write_redaction_recipe",
]