from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PlaceFields(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    canonical_name: str | None = Field(default=None, max_length=300)
    address: str | None = Field(default=None, max_length=500)
    postal_code: str | None = Field(default=None, max_length=50)
    country: str | None = Field(default=None, max_length=100)
    province: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    district: str | None = Field(default=None, max_length=100)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_m: float = Field(default=200.0, gt=0)
    provider_place_id: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=100)
    active: bool = True
    favorite: bool = False

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("display_name cannot be blank")
        return value


class PlaceCreate(PlaceFields):
    pass


class PlaceUpdate(BaseModel):
    revision: int = Field(ge=1)
    display_name: str | None = Field(default=None, max_length=200)
    canonical_name: str | None = Field(default=None, max_length=300)
    address: str | None = Field(default=None, max_length=500)
    postal_code: str | None = Field(default=None, max_length=50)
    country: str | None = Field(default=None, max_length=100)
    province: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    district: str | None = Field(default=None, max_length=100)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_m: float | None = Field(default=None, gt=0)
    provider_place_id: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=100)
    active: bool | None = None
    favorite: bool | None = None

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("display_name cannot be blank")
        return value

    @model_validator(mode="after")
    def reject_null_required_values(self) -> "PlaceUpdate":
        for name in ("display_name", "latitude", "longitude", "radius_m", "active", "favorite"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class PlaceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    display_name: str
    canonical_name: str | None
    address: str | None
    postal_code: str | None
    country: str | None
    province: str | None
    city: str | None
    district: str | None
    latitude: float
    longitude: float
    radius_m: float
    provider_place_id: str | None
    category: str | None
    creation_source: str
    active: bool
    favorite: bool
    usage_count: int
    last_used_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class PlaceListResponse(BaseModel):
    items: list[PlaceResponse]
    total: int
    limit: int
    offset: int


PlaceMatchSource = Literal["PROVIDER_PLACE_ID", "CANONICAL_NAME", "RADIUS", "NONE"]


class PlaceMatchRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    provider_place_id: str | None = Field(default=None, max_length=255)
    canonical_name: str | None = Field(default=None, max_length=300)


class PlaceMatchResponse(BaseModel):
    matched: bool
    place: PlaceResponse | None = None
    distance_m: float | None = None
    match_source: PlaceMatchSource


class FilePlaceUpdate(BaseModel):
    memorykeeper_place_id: UUID | None
    expected_revision: int = Field(ge=0)


class FilePlaceResponse(BaseModel):
    file_id: str
    memorykeeper_place_id: UUID | None
    place_display_name: str | None
    place_canonical_name: str | None
    geocoded_place_name: str | None
    place_match_source: str | None
    place_match_distance_m: float | None
    place_revision: int


class ReclassifyRequest(BaseModel):
    reassign_from_other_places: bool = False


class ReclassifyResponse(BaseModel):
    place_id: UUID
    scanned: int
    assigned: int
    reassigned: int
    unassigned_outside_radius: int
    unchanged: int


class RadiusImpactRequest(BaseModel):
    place_id: UUID | None = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_m: float = Field(gt=0)


class PlaceOverlap(BaseModel):
    place: PlaceResponse
    center_distance_m: float


class RadiusImpactResponse(BaseModel):
    matched_file_count: int
    affected_file_ids: list[str]
    overlapping_places: list[PlaceOverlap]
