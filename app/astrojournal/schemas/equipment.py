from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


EquipmentKind = Literal["smartTelescope", "refractor", "reflector", "other"]
EquipmentPurpose = Literal["imaging", "visual"]


class DiscreteExposureCapability(BaseModel):
    type: Literal["discrete"]
    values_seconds: list[float] = Field(min_length=1)

    @field_validator("values_seconds")
    @classmethod
    def normalize_values(cls, value: list[float]) -> list[float]:
        if any(item <= 0 for item in value):
            raise ValueError("discrete exposure values must be positive")
        if len(set(value)) != len(value):
            raise ValueError("discrete exposure values must be unique")
        return sorted(value)


class RangeExposureCapability(BaseModel):
    type: Literal["range"]
    min_seconds: float = Field(gt=0)
    max_seconds: float = Field(gt=0)
    step_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "RangeExposureCapability":
        if self.max_seconds < self.min_seconds:
            raise ValueError("max_seconds must be greater than or equal to min_seconds")
        return self


ExposureCapability = Annotated[
    DiscreteExposureCapability | RangeExposureCapability,
    Field(discriminator="type"),
]


class EyepieceWrite(BaseModel):
    id: UUID
    equipment_id: UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    focal_length_mm: float = Field(gt=0)
    afov_degrees: float = Field(gt=0)
    sort_order: int = 0

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value


class EyepieceResponse(EyepieceWrite):
    equipment_id: UUID


class EquipmentCreate(BaseModel):
    id: UUID
    name: str = Field(min_length=1, max_length=200)
    kind: EquipmentKind
    purpose: EquipmentPurpose
    is_active: bool = True
    focal_length_mm: float | None = Field(default=None, gt=0)
    aperture_mm: float | None = Field(default=None, gt=0)
    fov_width_degrees: float | None = Field(default=None, gt=0)
    fov_height_degrees: float | None = Field(default=None, gt=0)
    sort_order: int = 0
    eyepieces: list[EyepieceWrite] = Field(default_factory=list)
    az_exposure_capability: ExposureCapability | None = None
    eq_exposure_capability: ExposureCapability | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_children(self) -> "EquipmentCreate":
        equipment_id = str(self.id)
        for child in self.eyepieces:
            if child.equipment_id is not None and str(child.equipment_id) != equipment_id:
                raise ValueError("child equipment_id must match equipment id")
        child_ids = [item.id for item in self.eyepieces]
        if len(set(child_ids)) != len(child_ids):
            raise ValueError("eyepiece ids must be unique")
        return self


class EquipmentUpdate(BaseModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=200)
    kind: EquipmentKind | None = None
    purpose: EquipmentPurpose | None = None
    is_active: bool | None = None
    focal_length_mm: float | None = Field(default=None, gt=0)
    aperture_mm: float | None = Field(default=None, gt=0)
    fov_width_degrees: float | None = Field(default=None, gt=0)
    fov_height_degrees: float | None = Field(default=None, gt=0)
    sort_order: int | None = None
    eyepieces: list[EyepieceWrite] | None = None
    az_exposure_capability: ExposureCapability | None = None
    eq_exposure_capability: ExposureCapability | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_patch(self) -> "EquipmentUpdate":
        changed = self.model_fields_set - {"expected_revision"}
        if not changed:
            raise ValueError("at least one mutable field is required")
        for name in ("name", "kind", "purpose", "is_active", "sort_order", "eyepieces"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class EquipmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    kind: EquipmentKind
    purpose: EquipmentPurpose
    is_active: bool
    focal_length_mm: float | None
    aperture_mm: float | None
    fov_width_degrees: float | None
    fov_height_degrees: float | None
    sort_order: int
    eyepieces: list[EyepieceResponse]
    az_exposure_capability: ExposureCapability | None
    eq_exposure_capability: ExposureCapability | None
    revision: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class EquipmentDeleteResponse(BaseModel):
    equipment_id: UUID
    deleted: bool
    revision: int
    deleted_at: datetime


class EquipmentConflictDetail(BaseModel):
    code: str
    equipment_id: UUID
    expected_revision: int
    current_revision: int


class EquipmentConflictResponse(BaseModel):
    detail: EquipmentConflictDetail
