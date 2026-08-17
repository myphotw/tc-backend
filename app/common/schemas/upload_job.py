"""Upload Job Status API schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class UploadJobStatusResponse(BaseModel):
    """Upload Job 상태 응답."""

    model_config = ConfigDict(from_attributes=True)

    job_id: str
    status: str
    progress: int = Field(ge=0, le=100)
    current_plugin: str | None = None
    processing_log: str | None = None
    retry_count: int = 0
    requested_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_error: str | None = None
    updated_at: datetime | None = None
    service_name: str | None = None
    client_file_id: str | None = None
    backend_file_id: str | None = None
    common_file_id: int | None = None


class UploadJobListResponse(BaseModel):
    """Upload Job 목록 응답."""

    items: list[UploadJobStatusResponse]
    page: int
    page_size: int
    total: int
    sort: str
