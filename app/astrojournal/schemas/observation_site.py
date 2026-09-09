from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


HorizonSource = Literal["manual", "camera_scan", "photo_import", "video_import"]
TrackingMode = Literal["altAz", "eq"]
_LOCAL_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _normalize_local_time(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if not _LOCAL_TIME.fullmatch(value):
            raise ValueError("preferred time must use 24-hour HH:mm format")
    return value


class HorizonPointWrite(BaseModel):
    id: UUID
    observation_site_id: UUID | None = None
    azimuth: float = Field(ge=0, lt=360)
    min_altitude: float = Field(ge=-90, le=90)
    max_altitude: float | None = Field(default=None, ge=-90, le=90)
    sort_order: int = 0
    source: HorizonSource = "manual"

    @model_validator(mode="after")
    def validate_altitudes(self) -> "HorizonPointWrite":
        if self.max_altitude is not None and self.max_altitude < self.min_altitude:
            raise ValueError("max_altitude must be greater than or equal to min_altitude")
        return self


class HorizonPointResponse(HorizonPointWrite):
    observation_site_id: UUID


class BlockedAzimuthRangeWrite(BaseModel):
    id: UUID
    observation_site_id: UUID | None = None
    start_azimuth: float = Field(ge=0, lt=360)
    end_azimuth: float = Field(ge=0, lt=360)
    reason: str | None = Field(default=None, max_length=500)
    source: HorizonSource = "manual"


class BlockedAzimuthRangeResponse(BlockedAzimuthRangeWrite):
    observation_site_id: UUID


class ObservationSiteCreate(BaseModel):
    id: UUID
    name: str = Field(min_length=1, max_length=200)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    address: str | None = Field(default=None, max_length=500)
    bortle: int | None = Field(default=None, ge=1, le=9)
    sqm: float | None = None
    brightness_grade: str | None = Field(default=None, max_length=100)
    is_favorite: bool = True
    tracking_mode: TrackingMode = "altAz"
    default_equipment_id: UUID | None = None
    default_min_altitude: float = Field(default=20.0, ge=-90, le=90)
    default_max_altitude: float | None = Field(default=None, ge=-90, le=90)
    preferred_start: str | None = None
    preferred_end: str | None = None
    memo: str = ""
    horizon_points: list[HorizonPointWrite] = Field(default_factory=list)
    blocked_azimuth_ranges: list[BlockedAzimuthRangeWrite] = Field(
        default_factory=list
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @field_validator("address", "brightness_grade", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("preferred_start", "preferred_end", mode="before")
    @classmethod
    def validate_local_time(cls, value: Any) -> Any:
        return _normalize_local_time(value)

    @model_validator(mode="after")
    def validate_aggregate(self) -> "ObservationSiteCreate":
        if (
            self.default_max_altitude is not None
            and self.default_max_altitude < self.default_min_altitude
        ):
            raise ValueError(
                "default_max_altitude must be greater than or equal to "
                "default_min_altitude"
            )
        site_id = str(self.id)
        for child in (*self.horizon_points, *self.blocked_azimuth_ranges):
            if (
                child.observation_site_id is not None
                and str(child.observation_site_id) != site_id
            ):
                raise ValueError("child observation_site_id must match site id")
        horizon_ids = [item.id for item in self.horizon_points]
        if len(set(horizon_ids)) != len(horizon_ids):
            raise ValueError("horizon point ids must be unique")
        azimuths = [item.azimuth for item in self.horizon_points]
        if len(set(azimuths)) != len(azimuths):
            raise ValueError("horizon point azimuths must be unique")
        range_ids = [item.id for item in self.blocked_azimuth_ranges]
        if len(set(range_ids)) != len(range_ids):
            raise ValueError("blocked range ids must be unique")
        return self


class ObservationSiteUpdate(BaseModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=200)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    address: str | None = Field(default=None, max_length=500)
    bortle: int | None = Field(default=None, ge=1, le=9)
    sqm: float | None = None
    brightness_grade: str | None = Field(default=None, max_length=100)
    is_favorite: bool | None = None
    tracking_mode: TrackingMode | None = None
    default_equipment_id: UUID | None = None
    default_min_altitude: float | None = Field(default=None, ge=-90, le=90)
    default_max_altitude: float | None = Field(default=None, ge=-90, le=90)
    preferred_start: str | None = None
    preferred_end: str | None = None
    memo: str | None = None
    horizon_points: list[HorizonPointWrite] | None = None
    blocked_azimuth_ranges: list[BlockedAzimuthRangeWrite] | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @field_validator("address", "brightness_grade", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("preferred_start", "preferred_end", mode="before")
    @classmethod
    def validate_local_time(cls, value: Any) -> Any:
        return _normalize_local_time(value)

    @model_validator(mode="after")
    def validate_patch(self) -> "ObservationSiteUpdate":
        changed = self.model_fields_set - {"expected_revision"}
        if not changed:
            raise ValueError("at least one mutable field is required")
        for name in (
            "name",
            "latitude",
            "longitude",
            "is_favorite",
            "tracking_mode",
            "default_min_altitude",
            "memo",
            "horizon_points",
            "blocked_azimuth_ranges",
        ):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class ObservationSiteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    latitude: float
    longitude: float
    address: str | None
    bortle: int | None
    sqm: float | None
    brightness_grade: str | None
    is_favorite: bool
    tracking_mode: TrackingMode
    default_equipment_id: UUID | None
    default_min_altitude: float
    default_max_altitude: float | None
    preferred_start: str | None
    preferred_end: str | None
    memo: str
    horizon_points: list[HorizonPointResponse]
    blocked_azimuth_ranges: list[BlockedAzimuthRangeResponse]
    revision: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class ObservationSiteDeleteResponse(BaseModel):
    site_id: UUID
    deleted: bool
    revision: int
    deleted_at: datetime


class ObservationSiteConflictDetail(BaseModel):
    code: str
    site_id: UUID
    expected_revision: int
    current_revision: int


class ObservationSiteConflictResponse(BaseModel):
    detail: ObservationSiteConflictDetail
