from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class AstroJournalResetPreviewResponse(BaseModel):
    observation_record_count: int
    astro_file_count: int
    astro_only_file_count: int
    shared_file_count: int
    plate_solve_result_count: int
    photo_object_count: int
    upload_job_count: int
    pending_upload_count: int
    processing_upload_count: int
    processing_vision_job_count: int
    processing_job_count: int
    physical_original_delete_count: int
    physical_preview_delete_count: int
    physical_thumbnail_delete_count: int
    preserved_shared_file_count: int
    reset_blocked: bool
    blocked_reason: str | None = None


class AstroJournalResetExecuteRequest(BaseModel):
    confirmation: Literal["RESET_ASTROJOURNAL"]


class AstroJournalResetExecuteResponse(BaseModel):
    reset_completed: bool
    deleted_observation_record_count: int
    removed_astro_file_link_count: int
    tombstoned_common_file_count: int
    preserved_shared_file_count: int
    deleted_upload_job_count: int
    deleted_original_count: int
    deleted_preview_count: int
    deleted_thumbnail_count: int
    deleted_plate_solve_result_count: int
    deleted_photo_object_count: int
    reset_event_cursor: int
