"""Opaque cursor codec for MemoryKeeper fast Gallery keyset pagination."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status


_CURSOR_VERSION = 2
_DATE_UNCLASSIFIED_CURSOR_VERSION = 1


@dataclass(frozen=True)
class FastGalleryCursor:
    effective_capture_year: int
    effective_capture_datetime: datetime | None
    file_id: int


@dataclass(frozen=True)
class FastGalleryDateUnclassifiedCursor:
    file_id: int


def encode_cursor(cursor: FastGalleryCursor) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "effective_capture_year": cursor.effective_capture_year,
        "effective_capture_datetime": (
            cursor.effective_capture_datetime.isoformat(timespec="microseconds")
            if cursor.effective_capture_datetime is not None
            else None
        ),
        "file_id": cursor.file_id,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def encode_date_unclassified_cursor(
    cursor: FastGalleryDateUnclassifiedCursor,
) -> str:
    payload = {
        "v": _DATE_UNCLASSIFIED_CURSOR_VERSION,
        "mode": "date_unclassified",
        "file_id": cursor.file_id,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> FastGalleryCursor:
    """Decode mixed-precision cursors and legacy exact-date cursors."""
    try:
        padded = value + ("=" * (-len(value) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("unexpected cursor fields")
        version = payload.get("v")
        legacy_fields = {"v", "effective_capture_datetime", "file_id"}
        current_fields = {
            "v",
            "effective_capture_year",
            "effective_capture_datetime",
            "file_id",
        }
        if version == 1 and set(payload) == legacy_fields:
            captured_at = datetime.fromisoformat(
                str(payload["effective_capture_datetime"])
            )
            capture_year = captured_at.year
        elif version == _CURSOR_VERSION and set(payload) == current_fields:
            raw_captured_at = payload["effective_capture_datetime"]
            captured_at = (
                datetime.fromisoformat(str(raw_captured_at))
                if raw_captured_at is not None
                else None
            )
            capture_year = payload["effective_capture_year"]
        else:
            raise ValueError("unsupported cursor version")
        file_id = payload["file_id"]
        if (
            (captured_at is not None and captured_at.tzinfo is not None)
            or (captured_at is not None and captured_at.utcoffset() is not None)
            or not isinstance(capture_year, int)
            or isinstance(capture_year, bool)
            or capture_year <= 0
            or not isinstance(file_id, int)
            or isinstance(file_id, bool)
            or file_id <= 0
        ):
            raise ValueError("invalid cursor values")
        return FastGalleryCursor(capture_year, captured_at, file_id)
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


def decode_date_unclassified_cursor(
    value: str,
) -> FastGalleryDateUnclassifiedCursor:
    """Decode the file-id keyset used only by YEAR-only Gallery reads."""
    try:
        padded = value + ("=" * (-len(value) % 4))
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "v",
            "mode",
            "file_id",
        }:
            raise ValueError("unexpected cursor fields")
        file_id = payload["file_id"]
        if (
            payload["v"] != _DATE_UNCLASSIFIED_CURSOR_VERSION
            or payload["mode"] != "date_unclassified"
            or not isinstance(file_id, int)
            or isinstance(file_id, bool)
            or file_id <= 0
        ):
            raise ValueError("invalid cursor values")
        return FastGalleryDateUnclassifiedCursor(file_id)
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
