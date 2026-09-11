"""Opaque identities and keyset cursors for MemoryKeeper cleanup queues."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status


_VERSION = 1


def _encode(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> dict[str, Any]:
    try:
        padded = value + ("=" * (-len(value) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != _VERSION:
            raise ValueError("unsupported payload")
        return payload
    except (UnicodeDecodeError, binascii.Error, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_CLEANUP_CURSOR", "message": "Invalid cleanup cursor"},
        ) from exc


def encode_group_id(queue: str, key: dict[str, Any]) -> str:
    return _encode({"v": _VERSION, "queue": queue, "key": key})


def decode_group_id(value: str, queue: str) -> dict[str, Any]:
    try:
        payload = _decode(value)
        if set(payload) != {"v", "queue", "key"} or payload["queue"] != queue:
            raise ValueError("wrong group kind")
        key = payload["key"]
        if not isinstance(key, dict):
            raise ValueError("invalid group key")
        return key
    except (HTTPException, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CLEANUP_GROUP_NOT_FOUND", "message": "Cleanup group not found"},
        ) from exc


def encode_page_cursor(
    captured_at: datetime | None,
    tie_breaker: int,
    *,
    queue: str,
    level: str,
) -> str:
    return _encode(
        {
            "v": _VERSION,
            "queue": queue,
            "level": level,
            "captured_at": captured_at.isoformat(timespec="microseconds") if captured_at else None,
            "tie_breaker": tie_breaker,
        }
    )


def decode_page_cursor(
    value: str,
    *,
    queue: str,
    level: str,
) -> tuple[datetime | None, int]:
    payload = _decode(value)
    if (
        set(payload) != {"v", "queue", "level", "captured_at", "tie_breaker"}
        or payload["queue"] != queue
        or payload["level"] != level
    ):
        raise HTTPException(status_code=400, detail={"code": "INVALID_CLEANUP_CURSOR"})
    try:
        captured_at = (
            datetime.fromisoformat(payload["captured_at"])
            if payload["captured_at"] is not None
            else None
        )
        tie_breaker = payload["tie_breaker"]
        if (
            (captured_at is not None and captured_at.tzinfo is not None)
            or not isinstance(tie_breaker, int)
            or isinstance(tie_breaker, bool)
            or tie_breaker <= 0
        ):
            raise ValueError("invalid cursor values")
        return captured_at, tie_breaker
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_CLEANUP_CURSOR", "message": "Invalid cleanup cursor"},
        ) from exc
