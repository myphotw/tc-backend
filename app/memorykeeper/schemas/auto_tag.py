from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AutoTagStatusResponse(BaseModel):
    service_available: bool
    credential_ready: bool
    worker_online: bool
    quota_available: bool
    monthly_limit_reached: bool
    quota_waiting_count: int
    waiting_count: int
    processing_count: int
    failed_count: int
    today_completed_count: int
    monthly_usage: int
    monthly_limit: int
    monthly_remaining: int
    curation_version: int
    last_processed_at: datetime | None = None
    last_failure_at: datetime | None = None


class AutoTagFailedJobItem(BaseModel):
    job_id: int
    file_id: str
    failed_at: datetime | None = None
    retry_count: int
    safe_error_code: str
    retryable: bool


class AutoTagFailedJobListResponse(BaseModel):
    items: list[AutoTagFailedJobItem]
    total: int
    page: int
    page_size: int


class AutoTagRetryResponse(BaseModel):
    requested_count: int
    requeued_count: int
    skipped_count: int
    failed_count: int


class AutoTagCurationPreviewResponse(BaseModel):
    service_name: str
    curation_version: int
    files_with_raw_tags: int
    current_raw_tag_count: int
    evaluated_file_count: int
    projected_curated_tag_count: int
    zero_tag_file_count: int
    mapped_percentage: float = Field(ge=0, le=100)
    sample_limit: int
    has_more: bool
