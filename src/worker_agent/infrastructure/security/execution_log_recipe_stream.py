"""Length-prefixed, ephemeral transfer of redaction secrets.

This module does not start processes, write files, or provide timeouts. A
caller must use a private stream and bound its own read/write and handoff
latency. In particular, a successful encode alone does not prove that the
consumer has received the secrets. The four-byte header is an unsigned
big-endian payload length; exactly that many UTF-8 JSON bytes follow.
"""

from __future__ import annotations

import json
import struct
from typing import BinaryIO

try:
    from .execution_log_recipe import MAX_RECIPE_BYTES, MAX_RECIPE_ITEMS
except ImportError:  # Directly executed source-mode child, with sibling on sys.path.
    from execution_log_recipe import MAX_RECIPE_BYTES, MAX_RECIPE_ITEMS


class RecipeFrameError(ValueError):
    """A recipe frame was malformed, unavailable, or exceeded its bounds."""


def _validate_values(values: object) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_RECIPE_ITEMS:
        raise RecipeFrameError("redaction recipe frame is invalid")
    if any(not isinstance(value, str) or len(value) > MAX_RECIPE_BYTES for value in values):
        raise RecipeFrameError("redaction recipe frame is invalid")
    return values


def encode_recipe_frame(values: object) -> bytes:
    """Encode a bounded list of secrets without writing it to persistent storage."""
    recipe = _validate_values(values)
    payload = bytearray()

    def append(part: bytes) -> None:
        if len(payload) + len(part) > MAX_RECIPE_BYTES:
            raise RecipeFrameError("redaction recipe frame exceeds its permitted size")
        payload.extend(part)

    try:
        append(b"[")
        for index, value in enumerate(recipe):
            if index:
                append(b",")
            append(b'"')
            # JSON escapes are independent for each code point. Encode in
            # small slices so a single value cannot allocate an unbounded
            # escaped temporary before the byte limit is checked.
            for offset in range(0, len(value), 4096):
                encoded = json.dumps(value[offset:offset + 4096], ensure_ascii=False)
                append(encoded[1:-1].encode("utf-8"))
            append(b'"')
        append(b"]")
    except RecipeFrameError:
        raise
    except (UnicodeError, ValueError, TypeError):
        raise RecipeFrameError("redaction recipe frame could not be encoded") from None
    return struct.pack("!I", len(payload)) + payload


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    payload = bytearray()
    remaining = count
    while remaining:
        request = min(remaining, 65536)
        try:
            chunk = stream.read(request)
        except Exception:
            raise RecipeFrameError("redaction recipe stream could not be read") from None
        if not isinstance(chunk, bytes) or not chunk or len(chunk) > request:
            raise RecipeFrameError("redaction recipe frame was incomplete or invalid")
        payload.extend(chunk)
        remaining -= len(chunk)
    return bytes(payload)


def read_recipe_frame(stream: BinaryIO) -> list[str]:
    """Consume exactly one frame, leaving subsequent stream bytes untouched."""
    header = _read_exact(stream, 4)
    size = struct.unpack("!I", header)[0]
    if size > MAX_RECIPE_BYTES:
        raise RecipeFrameError("redaction recipe frame exceeds its permitted size")
    payload = _read_exact(stream, size)
    try:
        values = json.loads(payload.decode("utf-8"))
        recipe = _validate_values(values)
        # JSON escape sequences can otherwise produce unpaired surrogates even
        # when the encoded input itself is valid UTF-8.
        for value in recipe:
            value.encode("utf-8")
        return recipe
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise RecipeFrameError("redaction recipe frame was invalid") from None
