"""Opaque cursor codec for MemoryKeeper fast Gallery keyset pagination."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status


_CURSOR_VERSION = 1


@dataclass(frozen=True)
class FastGalleryCursor:
    effective_capture_datetime: datetime
    file_id: int


def encode_cursor(cursor: FastGalleryCursor) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "effective_capture_datetime": cursor.effective_capture_datetime.isoformat(
            timespec="microseconds"
        ),
        "file_id": cursor.file_id,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> FastGalleryCursor:
    """Decode a cursor, rejecting malformed or timezone-aware values as 400."""
    try:
        padded = value + ("=" * (-len(value) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "v",
            "effective_capture_datetime",
            "file_id",
        }:
            raise ValueError("unexpected cursor fields")
        if payload["v"] != _CURSOR_VERSION:
            raise ValueError("unsupported cursor version")
        captured_at = datetime.fromisoformat(str(payload["effective_capture_datetime"]))
        file_id = payload["file_id"]
        if (
            captured_at.tzinfo is not None
            or captured_at.utcoffset() is not None
            or not isinstance(file_id, int)
            or isinstance(file_id, bool)
            or file_id <= 0
        ):
            raise ValueError("invalid cursor values")
        return FastGalleryCursor(captured_at, file_id)
    except (
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_GALLERY_CURSOR", "message": "Invalid gallery cursor"},
        ) from exc
