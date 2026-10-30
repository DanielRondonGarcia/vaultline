"""Focused execution-log redaction component (S6, ``Simplify before integration``).

Contains the incremental :class:`StreamRedactor` and the private constants
and helpers strictly necessary for its chunk-boundary redaction gate; no
other persistence, spawn or retention logic lives here, so the sibling
``execution_log_capture`` module stays the single capture entrypoint (script
and frozen dispatch, Docker and stdin modes).

Security invariants (unchanged): nothing is emitted before redaction - byte
chunks decode incrementally so secrets straddling chunk boundaries are seen
whole, tails that could still complete a secret occurrence or a URL scheme
head are held back, and an unterminated scheme marker holds its whole URL
head until the URL completes, the stream ends (``flush`` releases and
redacts it) or the bounded-memory limit drops it (``feed`` reports the
drop); later chunks stay discarded until the token terminator arrives. A
scheme glued to a preceding word character (``prefixhttps://``) is redacted
too: the ``://`` literal and the ``rclone:``/``local:`` tokens anchor the
repository URL matches instead of a word-boundary claim.
"""

from __future__ import annotations

import codecs
import re
from typing import Iterable, Tuple


class StreamRedactor:
    """Incremental redaction gate between raw executor chunks and the log file.

    Byte chunks decode incrementally so secrets straddling chunk boundaries
    are seen whole; tails that could still complete a secret or a URL scheme
    head are held back, and an unterminated scheme marker holds its whole URL
    head until the URL completes, the stream ends (``flush`` releases and
    redacts it) or the bounded-memory limit drops it (``feed`` reports the
    drop); later chunks stay discarded until the token terminator arrives.
    """

    MAX_PENDING_CHARS = 1 << 16
    MAX_PENDING_OVERFLOW_EVENTS = 16

    _URL_SCHEMES = ("http", "https", "s3", "gs", "az", "swift", "rclone", "local")
    # Chunk-boundary hold-back: any tail that is a prefix of a known scheme
    # or of scheme + separator must be held until complete (or dropped).
    _URL_HOLD_PREFIXES = frozenset(
        {
            scheme[:length].lower()
            for scheme in _URL_SCHEMES
            for length in range(1, len(scheme) + 1)
        }
        | {
            (scheme + tail).lower()
            for scheme in _URL_SCHEMES
            for tail in (":", ":/", "://")
        }
    )
    # Mirror the repository URL redaction of DockerRuntimeAdapter._redact_text.
    # No word-boundary claim before the schemes: a scheme glued to a preceding
    # word character (``prefixhttps://``) still carries the URL's credentials
    # and must be redacted. The matches stay anchored by the ``://`` literal
    # and by the scheme-specific ``rclone:``/``local:`` tokens themselves.
    _REPOSITORY_URL_PATTERNS = (
        re.compile(r"(?i)(?:https?|s3|gs|az|swift)://[^\s\"'<>]+"),
        re.compile(r"(?i)rclone:[^\s\"'<>]+"),
        re.compile(r"(?i)local:[^\s\"'<>]+"),
    )
    _URL_OPEN_MARKER_PATTERN = re.compile(r"(?i)(?:https?|s3|gs|az|swift)://|rclone:|local:")
    _URL_TERMINATOR_PATTERN = re.compile(r"[\s\"'<>]")

    _REDACTED = "<redacted>"

    def __init__(self, secrets: Iterable):
        self.secrets = tuple(dict.fromkeys(secret for secret in secrets if isinstance(secret, str) and secret))
        self._max_overlap = max((len(secret) - 1 for secret in self.secrets), default=0)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending: str = ""
        self._pending_overflow_events = 0
        self._suppressing = False

    @classmethod
    def redact_text(cls, value: object, secrets: Iterable) -> str:
        """One-shot redaction for whole texts: secrets longest-first, then
        repository URLs (no explicit secret needed)."""
        text = value.decode("utf-8", errors="replace") if isinstance(value, (bytes, bytearray)) else str(value or "")
        for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
            text = text.replace(secret, cls._REDACTED)
        for pattern in cls._REPOSITORY_URL_PATTERNS:
            text = pattern.sub("<redacted-repository>", text)
        return text

    @classmethod
    def _safe_emit_cutoff(cls, text: str, secrets: Tuple[str, ...], max_overlap: int) -> int:
        """Length of the ``text`` prefix that may be persisted, holding every
        tail that could still complete a secret occurrence or a URL head."""
        held = 0
        for secret in secrets:
            if not secret:
                continue
            limit = min(len(secret) - 1, max_overlap, len(text))
            for overlap in range(limit, 0, -1):
                if text.endswith(secret[:overlap]):
                    held = max(held, overlap)
                    break
        for length in range(min(9, len(text)), 0, -1):
            if text[-length:].lower() in cls._URL_HOLD_PREFIXES:
                held = max(held, length)
                break
        cutoff = len(text) - held
        if held < len(text):
            open_marker = None
            for match in cls._URL_OPEN_MARKER_PATTERN.finditer(text):
                open_marker = match
            if open_marker is not None and not cls._URL_TERMINATOR_PATTERN.search(text[open_marker.end():]):
                cutoff = min(cutoff, open_marker.start())  # hold the URL head
        while True:  # pull the cutoff past secret occurrences crossing it
            crossing = cutoff
            for secret in secrets:
                position = text.find(secret)
                while position >= 0:
                    end = position + len(secret)
                    if position < cutoff < end:
                        crossing = max(crossing, end)
                    position = text.find(secret, position + 1)
            if crossing == cutoff:
                return max(0, cutoff)
            cutoff = crossing

    def feed(self, raw: object) -> Tuple[str, bool]:
        """Redact one raw chunk; return the text safe to persist and whether
        an unterminated token was dropped at the memory bound."""
        if isinstance(raw, (bytes, bytearray)):
            text = self._decoder.decode(bytes(raw))
        elif isinstance(raw, str):
            text = raw
        else:
            text = str(raw)
        if self._suppressing:
            terminated = self._URL_TERMINATOR_PATTERN.search(text)
            if terminated is None:
                # Discarded state carries across chunk boundaries until the
                # unterminated token's terminator arrives; the dropped prefix
                # is still decoded to keep the incremental decoder's state.
                return "", False
            self._suppressing = False
            text = text[terminated.start():]
        self._pending += text
        cutoff = self._safe_emit_cutoff(self._pending, self.secrets, self._max_overlap)
        emitted = self._pending[:cutoff]
        self._pending = self._pending[cutoff:]
        overflowed = False
        if len(self._pending) > self.MAX_PENDING_CHARS:
            # Fail closed: drop the unterminated token, never persist it.
            self._pending = ""
            self._suppressing = True
            if self._pending_overflow_events < self.MAX_PENDING_OVERFLOW_EVENTS:
                self._pending_overflow_events += 1
                overflowed = True
        return emitted, overflowed

    def flush(self) -> str:
        """Release the retained tail before close (the stream cannot continue)."""
        pending, self._pending = self._pending, ""
        return pending

    def discard(self) -> None:
        """Drop the retained tail without persisting it (truncation path)."""
        self._pending = ""


__all__ = ["StreamRedactor"]