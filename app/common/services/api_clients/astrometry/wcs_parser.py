"""Small FITS WCS header parser for Astrometry.net artifacts."""

from __future__ import annotations

import math
import re
from typing import Any


FITS_CARD_SIZE = 80
FITS_BLOCK_SIZE = 2880
MAX_WCS_FILE_BYTES = 1024 * 1024

_SIP_COEFFICIENT = re.compile(r"^(A|B|AP|BP)_(\d+)_(\d+)$")
_INTEGER = re.compile(r"^[+-]?\d+$")


class WcsParseError(ValueError):
    """The provider artifact is not a supported FITS WCS header."""


def parse_wcs_fits(
    payload: bytes,
    *,
    max_size: int = MAX_WCS_FILE_BYTES,
) -> dict[str, Any]:
    """Parse the primary FITS header into the versioned WCS API structure."""
    if not payload or len(payload) > max_size:
        raise WcsParseError("FITS WCS artifact size is invalid")
    if len(payload) < FITS_BLOCK_SIZE or len(payload) % FITS_BLOCK_SIZE != 0:
        raise WcsParseError("FITS WCS artifact is not block-aligned")
    if not payload.startswith(b"SIMPLE  ="):
        raise WcsParseError("FITS WCS artifact has no SIMPLE primary header")

    header = _parse_primary_header(payload)
    if header.get("SIMPLE") is not True:
        raise WcsParseError("FITS SIMPLE primary header is invalid")

    raster_width = _positive_int(
        header.get("IMAGEW", header.get("NAXIS1")),
        "IMAGEW/NAXIS1",
    )
    raster_height = _positive_int(
        header.get("IMAGEH", header.get("NAXIS2")),
        "IMAGEH/NAXIS2",
    )

    ctype1 = _required_string(header, "CTYPE1")
    ctype2 = _required_string(header, "CTYPE2")
    crval1 = _required_number(header, "CRVAL1")
    crval2 = _required_number(header, "CRVAL2")
    crpix1 = _required_number(header, "CRPIX1")
    crpix2 = _required_number(header, "CRPIX2")
    cd11, cd12, cd21, cd22 = _normalized_cd_matrix(header)

    sip = _parse_sip(header)
    return {
        "schema_version": 1,
        "ctype1": ctype1,
        "ctype2": ctype2,
        "cunit1": _optional_string(header.get("CUNIT1")),
        "cunit2": _optional_string(header.get("CUNIT2")),
        "radesys": _optional_string(header.get("RADESYS")),
        "equinox": _optional_number(header.get("EQUINOX"), "EQUINOX"),
        "lonpole": _optional_number(header.get("LONPOLE"), "LONPOLE"),
        "latpole": _optional_number(header.get("LATPOLE"), "LATPOLE"),
        "crval1": crval1,
        "crval2": crval2,
        "crpix1": crpix1,
        "crpix2": crpix2,
        "cd11": cd11,
        "cd12": cd12,
        "cd21": cd21,
        "cd22": cd22,
        "raster_width": raster_width,
        "raster_height": raster_height,
        "sip": sip,
    }


def _parse_primary_header(payload: bytes) -> dict[str, object]:
    values: dict[str, object] = {}
    end_found = False
    for offset in range(0, len(payload), FITS_CARD_SIZE):
        raw_card = payload[offset : offset + FITS_CARD_SIZE]
        try:
            card = raw_card.decode("ascii")
        except UnicodeDecodeError as exc:
            raise WcsParseError("FITS header contains non-ASCII data") from exc

        keyword = card[:8].strip()
        if keyword == "END":
            end_found = True
            break
        if not keyword or card[8:10] != "= ":
            continue
        values[keyword] = _parse_card_value(_without_comment(card[10:]))

    if not end_found:
        raise WcsParseError("FITS header has no END card")
    return values


def _without_comment(value: str) -> str:
    in_string = False
    index = 0
    while index < len(value):
        character = value[index]
        if character == "'":
            if in_string and index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            in_string = not in_string
        elif character == "/" and not in_string:
            return value[:index].strip()
        index += 1
    return value.strip()


def _parse_card_value(value: str) -> object:
    if not value:
        raise WcsParseError("FITS card has an empty value")
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise WcsParseError("FITS card has an unterminated string")
        return value[1:-1].replace("''", "'").rstrip()
    if value == "T":
        return True
    if value == "F":
        return False
    if _INTEGER.fullmatch(value):
        return int(value)
    try:
        number = float(value.replace("D", "E").replace("d", "e"))
    except ValueError as exc:
        raise WcsParseError(f"Unsupported FITS card value: {value[:32]}") from exc
    if not math.isfinite(number):
        raise WcsParseError("FITS numeric value must be finite")
    return number


def _normalized_cd_matrix(
    header: dict[str, object],
) -> tuple[float, float, float, float]:
    cd_keys = ("CD1_1", "CD1_2", "CD2_1", "CD2_2")
    present = tuple(key in header for key in cd_keys)
    if all(present):
        return (
            _required_number(header, "CD1_1"),
            _required_number(header, "CD1_2"),
            _required_number(header, "CD2_1"),
            _required_number(header, "CD2_2"),
        )
    if any(present):
        raise WcsParseError("FITS WCS contains a partial CD matrix")

    cdelt1 = _required_number(header, "CDELT1")
    cdelt2 = _required_number(header, "CDELT2")
    pc11 = _optional_number(header.get("PC1_1"), "PC1_1", default=1.0)
    pc12 = _optional_number(header.get("PC1_2"), "PC1_2", default=0.0)
    pc21 = _optional_number(header.get("PC2_1"), "PC2_1", default=0.0)
    pc22 = _optional_number(header.get("PC2_2"), "PC2_2", default=1.0)
    assert pc11 is not None and pc12 is not None
    assert pc21 is not None and pc22 is not None
    return (
        cdelt1 * pc11,
        cdelt1 * pc12,
        cdelt2 * pc21,
        cdelt2 * pc22,
    )


def _parse_sip(header: dict[str, object]) -> dict[str, Any] | None:
    orders: dict[str, int | None] = {}
    coefficients: dict[str, dict[str, float]] = {
        "a": {},
        "b": {},
        "ap": {},
        "bp": {},
    }
    for prefix in ("A", "B", "AP", "BP"):
        key = f"{prefix}_ORDER"
        orders[f"{prefix.lower()}_order"] = (
            _nonnegative_int(header[key], key) if key in header else None
        )

    for key, value in header.items():
        match = _SIP_COEFFICIENT.fullmatch(key)
        if match is None:
            continue
        prefix, first, second = match.groups()
        coefficients[prefix.lower()][f"{first}_{second}"] = _number(value, key)

    if not any(value is not None for value in orders.values()) and not any(
        coefficients.values()
    ):
        return None
    return {**orders, **coefficients}


def _required_string(header: dict[str, object], key: str) -> str:
    if key not in header:
        raise WcsParseError(f"FITS WCS is missing {key}")
    value = _optional_string(header[key])
    if value is None:
        raise WcsParseError(f"FITS WCS {key} is not a string")
    return value


def _optional_string(value: object | None) -> str | None:
    return value if isinstance(value, str) and value else None


def _required_number(header: dict[str, object], key: str) -> float:
    if key not in header:
        raise WcsParseError(f"FITS WCS is missing {key}")
    return _number(header[key], key)


def _optional_number(
    value: object | None,
    key: str,
    *,
    default: float | None = None,
) -> float | None:
    return default if value is None else _number(value, key)


def _number(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WcsParseError(f"FITS WCS {key} is not numeric")
    number = float(value)
    if not math.isfinite(number):
        raise WcsParseError(f"FITS WCS {key} must be finite")
    return number


def _positive_int(value: object | None, key: str) -> int:
    result = _nonnegative_int(value, key)
    if result <= 0:
        raise WcsParseError(f"FITS WCS {key} must be positive")
    return result


def _nonnegative_int(value: object | None, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WcsParseError(f"FITS WCS {key} is not an integer")
    result = int(value)
    if float(value) != result or result < 0:
        raise WcsParseError(f"FITS WCS {key} is not a non-negative integer")
    return result
