"""UploadJob log를 통한 최소 upload metadata 전달 계약."""

from __future__ import annotations

import json
from typing import Any


UPLOAD_METADATA_LOG_PREFIX = "UPLOAD_METADATA "
UPLOAD_METADATA_FIELDS = (
    "observation_date",
    "canonical_target_id",
    "target_display_name",
)


def encode_upload_metadata(metadata: dict[str, Any]) -> str | None:
    """허용된 upload metadata를 processing_log marker로 직렬화한다."""
    payload = {
        key: value.isoformat() if hasattr(value, "isoformat") else str(value)
        for key in UPLOAD_METADATA_FIELDS
        if (value := metadata.get(key)) is not None and str(value).strip()
    }
    if not payload:
        return None
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"{UPLOAD_METADATA_LOG_PREFIX}{encoded}\\n"


def decode_upload_metadata(processing_log: str | None) -> dict[str, str]:
    """processing_log의 마지막 유효 upload metadata marker를 복원한다."""
    if not processing_log:
        return {}

    lines = processing_log.replace("\\n", "\n").splitlines()
    for line in reversed(lines):
        if not line.startswith(UPLOAD_METADATA_LOG_PREFIX):
            continue
        try:
            payload = json.loads(line[len(UPLOAD_METADATA_LOG_PREFIX) :])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        return {
            key: str(payload[key])
            for key in UPLOAD_METADATA_FIELDS
            if payload.get(key) is not None and str(payload[key]).strip()
        }
    return {}
