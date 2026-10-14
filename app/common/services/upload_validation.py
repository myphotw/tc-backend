"""Stable validation outcomes for the upload API and worker pipeline."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


class UploadFailureCode:
    DUPLICATE_CONTENT = "DUPLICATE_CONTENT"
    CONTENT_SHA256_MISMATCH = "CONTENT_SHA256_MISMATCH"

    ALL = frozenset(
        {
            DUPLICATE_CONTENT,
            CONTENT_SHA256_MISMATCH,
        }
    )


class UploadValidationError(RuntimeError):
    """Fail one UploadJob with a stable, machine-readable reason code."""

    def __init__(self, code: str, message: str) -> None:
        if code not in UploadFailureCode.ALL:
            raise ValueError(f"Unsupported upload failure code: {code}")
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def extract_upload_failure_code(error_message: str | None) -> str | None:
    """Extract only known codes; arbitrary worker errors remain unclassified."""

    if not error_message:
        return None
    candidate = error_message.partition(":")[0].strip()
    return candidate if candidate in UploadFailureCode.ALL else None


def acquire_upload_sha_transaction_lock(
    db: Session,
    content_sha256: str,
) -> None:
    """Serialize one SHA's derivative/storage registration on PostgreSQL."""

    if db.get_bind().dialect.name != "postgresql":
        return
    lock_key = int(content_sha256[:16], 16)
    if lock_key >= 2**63:
        lock_key -= 2**64
    db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )
