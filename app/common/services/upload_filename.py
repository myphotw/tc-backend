"""Safe normalization for client-provided upload filenames."""

from __future__ import annotations

from email.header import decode_header


def decode_upload_filename(filename: str) -> str:
    """Decode RFC 2047 encoded words without weakening path sanitization."""
    value = str(filename or "")
    try:
        fragments = decode_header(value)
    except (LookupError, ValueError):
        fragments = [(value, None)]

    decoded: list[str] = []
    for fragment, charset in fragments:
        if isinstance(fragment, str):
            decoded.append(fragment)
            continue
        encoding = charset or "utf-8"
        try:
            decoded.append(fragment.decode(encoding, errors="strict"))
        except (LookupError, UnicodeDecodeError):
            decoded.append(fragment.decode("utf-8", errors="replace"))

    # Header folding characters have no valid role in a single filename.
    normalized = "".join(decoded).replace("\x00", "_").replace("\r", "_").replace("\n", "_")
    return normalized or value or "unknown"
