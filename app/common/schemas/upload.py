"""Upload request and response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class UploadPreflightRequest(BaseModel):
    service_name: Literal["MemoryKeeper"]
    upload_source: Literal["mobile"]
    client_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UploadPreflightResponse(BaseModel):
    duplicate: bool
    upload_allowed: bool
    reason: Literal["DUPLICATE_CONTENT"] | None = None
