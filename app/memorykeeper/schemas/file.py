from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


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
