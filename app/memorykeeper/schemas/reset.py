from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class MemoryKeeperResetPreviewResponse(BaseModel):
    memorykeeper_file_count: int
    place_count: int
    user_tag_count: int
    favorite_count: int
    memo_count: int
    file_tag_relation_count: int
    file_tag_suppression_count: int
    pending_count: int
    preserved_common_file_count: int
    preserved_raw_vision_count: int
    shared_with_other_service_count: int
    upload_job_count: int
    active_upload_job_count: int
    processing_vision_job_count: int
    reset_blocked: bool


class MemoryKeeperResetExecuteRequest(BaseModel):
    confirmation: Literal["RESET_MEMORYKEEPER"]


class MemoryKeeperResetExecuteResponse(BaseModel):
    reset_completed: bool
    affected_file_count: int
    removed_place_count: int
    removed_user_tag_count: int
    cleared_state_count: int
    preserved_common_file_count: int
    preserved_raw_vision_count: int
    reset_event_cursor: int
