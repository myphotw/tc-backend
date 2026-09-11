from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.memorykeeper.schemas.place import FilePlaceResponse


def _normalize_file_ids(value: list[str]) -> list[str]:
    normalized = [item.strip() for item in value]
    if any(not item for item in normalized):
        raise ValueError("file_id cannot be blank")
    if len(set(normalized)) != len(normalized):
        raise ValueError("file_ids must be unique")
    return normalized


class MemoryKeeperPlaceStateQueryRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=500)

    @field_validator("file_ids")
    @classmethod
    def unique_file_ids(cls, value: list[str]) -> list[str]:
        return _normalize_file_ids(value)


class MemoryKeeperPlaceStateItem(BaseModel):
    file_id: str
    common_file_id: int
    gps_lat: float | None
    gps_lon: float | None
    memorykeeper_place_id: UUID | None
    place_match_revision: int


class MemoryKeeperPlaceStateQueryResponse(BaseModel):
    items: list[MemoryKeeperPlaceStateItem]


class MemoryKeeperBatchAssignPlaceRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=500)
    memorykeeper_place_id: UUID
    expected_place_revisions: dict[str, int]

    @field_validator("file_ids")
    @classmethod
    def unique_file_ids(cls, value: list[str]) -> list[str]:
        return _normalize_file_ids(value)

    @model_validator(mode="after")
    def revisions_cover_files(self) -> "MemoryKeeperBatchAssignPlaceRequest":
        if set(self.expected_place_revisions) != set(self.file_ids):
            raise ValueError(
                "expected_place_revisions must contain exactly every file_id"
            )
        if any(value < 0 for value in self.expected_place_revisions.values()):
            raise ValueError("expected place revisions must be non-negative")
        return self


class MemoryKeeperBatchAssignPlaceResponse(BaseModel):
    items: list[FilePlaceResponse]
    assigned_count: int


class MemoryKeeperCaptureDateUpdateRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=500)
    user_capture_date: date | None
    expected_date_revisions: dict[str, int]

    @field_validator("file_ids")
    @classmethod
    def valid_unique_file_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.lower() for item in _normalize_file_ids(value)]
        if len(set(normalized)) != len(normalized):
            raise ValueError("file_ids must be unique")
        if any(re.fullmatch(r"[0-9a-fA-F]{64}", item) is None for item in normalized):
            raise ValueError("file_ids must contain SHA-256 identifiers")
        return normalized

    @model_validator(mode="after")
    def revisions_cover_files(self) -> "MemoryKeeperCaptureDateUpdateRequest":
        normalized_keys = [key.strip().lower() for key in self.expected_date_revisions]
        if len(set(normalized_keys)) != len(normalized_keys):
            raise ValueError("expected_date_revisions keys must be unique")
        normalized_revisions = {
            key.strip().lower(): value
            for key, value in self.expected_date_revisions.items()
        }
        if set(normalized_revisions) != set(self.file_ids):
            raise ValueError("expected_date_revisions must contain exactly every file_id")
        if any(value < 0 for value in normalized_revisions.values()):
            raise ValueError("expected date revisions must be non-negative")
        self.expected_date_revisions = normalized_revisions
        return self


class MemoryKeeperCaptureDateUpdateItem(BaseModel):
    file_id: str
    user_capture_datetime: datetime | None
    user_capture_precision: str | None
    effective_capture_datetime: datetime | None
    effective_capture_date: date | None
    effective_capture_year: int | None
    date_basis: str | None
    date_revision: int


class MemoryKeeperCaptureDateUpdateResponse(BaseModel):
    items: list[MemoryKeeperCaptureDateUpdateItem]
    updated_count: int


class MemoryKeeperFileMetadataUpdate(BaseModel):
    expected_revision: int = Field(ge=0)
    favorite: bool | None = None
    memo: str | None = Field(default=None, max_length=10_000)
    gps_lat: float | None = Field(default=None, ge=-90, le=90)
    gps_lon: float | None = Field(default=None, ge=-180, le=180)
    country: str | None = Field(default=None, max_length=100)
    province: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    district: str | None = Field(default=None, max_length=100)
    place_name: str | None = Field(default=None, max_length=200)

    @field_validator("memo", "country", "province", "city", "district", "place_name", mode="before")
    @classmethod
    def normalize_blank(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def validate_patch(self) -> "MemoryKeeperFileMetadataUpdate":
        changed = self.model_fields_set - {"expected_revision"}
        if not changed:
            raise ValueError("at least one mutable field is required")
        if "favorite" in self.model_fields_set and self.favorite is None:
            raise ValueError("favorite cannot be null")
        gps_fields = {"gps_lat", "gps_lon"}
        if changed & gps_fields and not gps_fields.issubset(self.model_fields_set):
            raise ValueError("gps_lat and gps_lon must be supplied together")
        if gps_fields.issubset(self.model_fields_set):
            if (self.gps_lat is None) != (self.gps_lon is None):
                raise ValueError("gps_lat and gps_lon must both be null or both be coordinates")
        return self


class MemoryKeeperFileMetadataResponse(BaseModel):
    file_id: str
    favorite: bool
    memo: str | None
    revision: int
    gps_lat: float | None
    gps_lon: float | None
    country: str | None
    province: str | None
    city: str | None
    district: str | None
    place_name: str | None
    memorykeeper_place_id: UUID | None
    place_match_source: str | None
    place_match_distance_m: float | None
    place_revision: int
    updated_at: datetime | None


class MemoryKeeperFileDeleteResponse(BaseModel):
    file_id: str
    cleanup_status: str
    physical_file_deleted: bool


class FileTagMutationRequest(BaseModel):
    expected_revision: int = Field(ge=0)


class FileTagMutationResponse(BaseModel):
    file_id: str
    tag_id: int
    assigned: bool
    revision: int


class FileTagVisibilityMutationResponse(BaseModel):
    file_id: str
    identity: str
    hidden: bool
    revision: int
