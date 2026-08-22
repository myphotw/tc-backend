from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.memorykeeper.schemas.place import FilePlaceResponse


class PendingFileItem(BaseModel):
    file_id: str
    thumbnail_url: str | None
    capture_datetime: datetime | None
    gps_lat: float | None
    gps_lon: float | None
    country: str | None
    province: str | None
    city: str | None
    district: str | None
    place_name: str | None
    memorykeeper_place_id: UUID | None = None
    place_revision: int
    suggested_place_id: UUID | None = None
    suggested_place_name: str | None = None
    suggested_match_source: str | None = None


class PendingListResponse(BaseModel):
    items: list[PendingFileItem]
    total: int
    page: int
    page_size: int


class PendingAssignPlaceRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=500)
    memorykeeper_place_id: UUID
    expected_revisions: dict[str, int]

    @field_validator("file_ids")
    @classmethod
    def unique_file_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("file_id cannot be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("file_ids must be unique")
        return normalized

    @model_validator(mode="after")
    def revisions_cover_files(self) -> "PendingAssignPlaceRequest":
        if set(self.expected_revisions) != set(self.file_ids):
            raise ValueError("expected_revisions must contain exactly every file_id")
        if any(value < 0 for value in self.expected_revisions.values()):
            raise ValueError("expected revisions must be non-negative")
        return self


class PendingAssignPlaceResponse(BaseModel):
    items: list[FilePlaceResponse]
    assigned_count: int
