from __future__ import annotations

import base64
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.memorykeeper.services.fast_gallery_location import (
    decode_location_key,
    encode_raw_location_key,
    encode_registered_location_key,
)


def _encoded_raw_payload(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    value = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
    return f"raw:v1:{value}"


def test_registered_location_key_round_trips_the_existing_place_uuid() -> None:
    place_id = str(uuid4())

    key = encode_registered_location_key(place_id)
    decoded = decode_location_key(key)

    assert key == f"registered:v1:{place_id}"
    assert decoded.kind == "registered"
    assert decoded.place_id == place_id


@pytest.mark.parametrize(
    ("country", "region", "place"),
    [
        ("일본", "Motobu", "40 Bise 海辺"),
        ("대한민국", "서울", "한강 공원"),
        (None, "", None),
    ],
)
def test_raw_location_key_round_trips_unicode_and_null_empty_values(
    country: str | None,
    region: str | None,
    place: str | None,
) -> None:
    key = encode_raw_location_key(country=country, region=region, place=place)

    decoded = decode_location_key(key)

    assert decoded.kind == "raw"
    assert decoded.country == country
    assert decoded.region == region
    assert decoded.place == place


@pytest.mark.parametrize(
    "key",
    [
        "other:v1:value",
        f"registered:v2:{uuid4()}",
        "registered:v1:not-a-uuid",
        "raw:v1:!!!!",
        "raw:v1:bm90LWpzb24",
        _encoded_raw_payload("not-an-object"),
        _encoded_raw_payload({"country": "일본", "region": "Motobu"}),
        _encoded_raw_payload(
            {"country": "일본", "region": "Motobu", "place": 40}
        ),
    ],
)
def test_malformed_location_key_returns_a_clear_client_error(key: str) -> None:
    with pytest.raises(HTTPException) as error:
        decode_location_key(key)

    assert error.value.status_code == 400
    assert error.value.detail["code"] == "INVALID_GALLERY_LOCATION_KEY"
