from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class CleanupGroupPage(BaseModel):
    next_cursor: str | None = None
    has_more: bool
    total_groups: int
    total_photos: int


class PlaceCleanupGroup(BaseModel):
    group_id: str
    issue_type: str
    title: str
    media_count: int
    first_effective_capture_datetime: datetime | None
    last_effective_capture_datetime: datetime | None
    estimated_location: str | None
    processing_status: str = "WAITING"
    representative_file_id: str
    representative_thumbnail_url: str | None


class PlaceCleanupGroupPage(CleanupGroupPage):
    items: list[PlaceCleanupGroup] = Field(default_factory=list)


class PlaceCleanupPhoto(BaseModel):
    file_id: str
    thumbnail_url: str | None
    effective_capture_datetime: datetime | None
    gps_lat: float | None
    gps_lon: float | None
    country: str | None
    province: str | None
    city: str | None
    district: str | None
    place_name: str | None
    memorykeeper_place_id: str | None
    place_revision: int


class PlaceCleanupPhotoPage(BaseModel):
    items: list[PlaceCleanupPhoto] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool
    total_photos: int


class CaptureDateCleanupGroup(BaseModel):
    group_id: str
    issue_type: str = "CAPTURE_DATE_REVIEW"
    title: str
    media_count: int
    first_effective_capture_datetime: datetime | None
    last_effective_capture_datetime: datetime | None
    cleanup_reason: str
    date_basis: str | None
    representative_file_id: str
    representative_thumbnail_url: str | None


class CaptureDateCleanupGroupPage(CleanupGroupPage):
    items: list[CaptureDateCleanupGroup] = Field(default_factory=list)


class CaptureDateCleanupPhoto(BaseModel):
    file_id: str
    raw_capture_datetime: datetime | None
    effective_capture_datetime: datetime | None
    effective_capture_date: date | None
    effective_capture_year: int | None
    date_basis: str | None
    date_cleanup_required: bool
    date_cleanup_reason: str
    user_capture_datetime: datetime | None
    user_capture_precision: str | None
    date_revision: int
    thumbnail_url: str | None


class CaptureDateCleanupPhotoPage(BaseModel):
    items: list[CaptureDateCleanupPhoto] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool
    total_photos: int
