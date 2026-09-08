"""Opaque location identity codec for MemoryKeeper Fast Gallery leaves."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from fastapi import HTTPException, status


_LOCATION_KEY_VERSION = "v1"
_MAX_LOCATION_KEY_LENGTH = 2048
_RAW_PAYLOAD_FIELDS = {"country", "place", "region"}


@dataclass(frozen=True)
class FastGalleryLocationIdentity:
    kind: Literal["registered", "raw"]
    place_id: str | None = None
    country: str | None = None
    region: str | None = None
    place: str | None = None


def encode_registered_location_key(place_id: str) -> str:
    """Return a stable key backed by an existing MemoryKeeper Place UUID."""
    canonical_place_id = str(UUID(place_id))
    return f"registered:{_LOCATION_KEY_VERSION}:{canonical_place_id}"


def encode_raw_location_key(
    *,
    country: str | None,
    region: str | None,
    place: str | None,
) -> str:
    """Return an opaque key for one effective raw hierarchy group."""
    payload = {"country": country, "place": place, "region": region}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    token = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
    return f"raw:{_LOCATION_KEY_VERSION}:{token}"


def decode_location_key(value: str) -> FastGalleryLocationIdentity:
    """Decode and strictly validate a client-provided location key."""
    try:
        if not value or len(value) > _MAX_LOCATION_KEY_LENGTH:
            raise ValueError("invalid location key length")
        kind, version, payload_value = value.split(":", 2)
        if version != _LOCATION_KEY_VERSION:
            raise ValueError("unsupported location key version")
        if kind == "registered":
            return FastGalleryLocationIdentity(
                kind="registered",
                place_id=str(UUID(payload_value)),
            )
        if kind != "raw":
            raise ValueError("unsupported location key kind")

        padded = payload_value + ("=" * (-len(payload_value) % 4))
        raw = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != _RAW_PAYLOAD_FIELDS:
            raise ValueError("unexpected raw location fields")
        if any(
            item is not None and not isinstance(item, str)
            for item in (
                payload["country"],
                payload["region"],
                payload["place"],
            )
        ):
            raise ValueError("invalid raw location values")
        return FastGalleryLocationIdentity(
            kind="raw",
            country=payload["country"],
            region=payload["region"],
            place=payload["place"],
        )
    except (
        UnicodeDecodeError,
        UnicodeEncodeError,
        binascii.Error,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_GALLERY_LOCATION_KEY",
                "message": "Invalid gallery location key",
            },
        ) from exc
