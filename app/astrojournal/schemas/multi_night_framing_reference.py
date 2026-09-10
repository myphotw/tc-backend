from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ReferenceBranch = Literal["rising", "setting"]
_FUTURE_CLOCK_SKEW = timedelta(minutes=15)


def _validate_reference_captured_at(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reference_captured_at must include a timezone offset")
    if value.astimezone(timezone.utc) > datetime.now(timezone.utc) + _FUTURE_CLOCK_SKEW:
        raise ValueError("reference_captured_at cannot be in the future")
    return value


class MultiNightFramingReferenceCreate(BaseModel):
    id: UUID
    catalog_object_id: str = Field(min_length=1, max_length=255)
    reference_captured_at: datetime
    site_id: UUID
    equipment_id: UUID
    reference_hour_angle_deg: float = Field(ge=-180, le=180)
    reference_parallactic_angle_deg: float = Field(ge=-180, le=180)
    reference_branch: ReferenceBranch

    @field_validator("catalog_object_id")
    @classmethod
    def normalize_catalog_object_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("catalog_object_id cannot be blank")
        return value

    @field_validator("reference_captured_at")
    @classmethod
    def validate_reference_captured_at(cls, value: datetime) -> datetime:
        return cast(datetime, _validate_reference_captured_at(value))


class MultiNightFramingReferenceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    reference_captured_at: datetime | None = None
    site_id: UUID | None = None
    equipment_id: UUID | None = None
    reference_hour_angle_deg: float | None = Field(default=None, ge=-180, le=180)
    reference_parallactic_angle_deg: float | None = Field(
        default=None,
        ge=-180,
        le=180,
    )
    reference_branch: ReferenceBranch | None = None

    @field_validator("reference_captured_at")
    @classmethod
    def validate_reference_captured_at(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return _validate_reference_captured_at(value)

    @model_validator(mode="after")
    def validate_patch(self) -> "MultiNightFramingReferenceUpdate":
        changed = self.model_fields_set - {"expected_revision"}
        if not changed:
            raise ValueError("at least one mutable field is required")
        for name in changed:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class MultiNightFramingReferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    catalog_object_id: str
    reference_captured_at: datetime
    site_id: UUID
    equipment_id: UUID
    reference_hour_angle_deg: float
    reference_parallactic_angle_deg: float
    reference_branch: ReferenceBranch
    revision: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class MultiNightFramingReferenceDeleteResponse(BaseModel):
    reference_id: UUID
    deleted: bool
    revision: int
    deleted_at: datetime


class MultiNightFramingReferenceConflictDetail(BaseModel):
    code: str
    reference_id: UUID
    expected_revision: int | None = None
    current_revision: int | None = None
    catalog_object_id: str | None = None
    equipment_id: UUID | None = None


class MultiNightFramingReferenceConflictResponse(BaseModel):
    detail: MultiNightFramingReferenceConflictDetail
