"""Small, bounded HTTP byte-range helpers for persisted media files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_SINGLE_BYTE_RANGE_RE = re.compile(r"^bytes=([0-9]*)-([0-9]*)$", re.IGNORECASE)
DEFAULT_STREAM_CHUNK_SIZE = 64 * 1024


class ByteRangeNotSatisfiable(ValueError):
    """The supplied Range header cannot be served as one byte range."""


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def parse_single_byte_range(value: str, *, file_size: int) -> ByteRange:
    """Parse one RFC-style byte range and clamp its end to the file size.

    Multipart ranges are deliberately rejected because the mobile streaming
    contract needs only one contiguous range.
    """
    if file_size < 0:
        raise ValueError("file_size must not be negative")

    normalized = value.strip()
    if "," in normalized:
        raise ByteRangeNotSatisfiable("multiple ranges are not supported")
    match = _SINGLE_BYTE_RANGE_RE.fullmatch(normalized)
    if match is None:
        raise ByteRangeNotSatisfiable("malformed byte range")

    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ByteRangeNotSatisfiable("empty byte range")
    if file_size == 0:
        raise ByteRangeNotSatisfiable("empty file has no satisfiable range")

    if not start_text:
        suffix_length = _parse_decimal(end_text)
        if suffix_length <= 0:
            raise ByteRangeNotSatisfiable("suffix length must be positive")
        start = max(file_size - suffix_length, 0)
        return ByteRange(start=start, end=file_size - 1)

    start = _parse_decimal(start_text)
    if start >= file_size:
        raise ByteRangeNotSatisfiable("range starts after end of file")
    if not end_text:
        return ByteRange(start=start, end=file_size - 1)

    end = _parse_decimal(end_text)
    if end < start:
        raise ByteRangeNotSatisfiable("range end precedes start")
    return ByteRange(start=start, end=min(end, file_size - 1))


def _parse_decimal(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ByteRangeNotSatisfiable("invalid byte position") from exc


def iter_file_range(
    path: Path,
    *,
    start: int,
    length: int,
    chunk_size: int = DEFAULT_STREAM_CHUNK_SIZE,
) -> Iterator[bytes]:
    """Yield at most ``length`` bytes and close the file on completion/cancel."""
    if start < 0 or length < 0:
        raise ValueError("start and length must not be negative")
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")

    remaining = length
    with path.open("rb") as source:
        source.seek(start)
        while remaining > 0:
            chunk = source.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
