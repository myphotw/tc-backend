from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ObservationRecordCreate(BaseModel):
    file_id: int
    catalog_object_id: str | None = Field(default=None, max_length=255)
    captured_at: datetime
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_name: str | None = Field(default=None, max_length=255)
    equipment_id: str | None = Field(default=None, max_length=255)
    memo: str | None = None
    favorite: bool = False
    representative: bool = False
    plate_solve_status: str = Field(default="PENDING", max_length=30)


class ObservationRecordUpdate(BaseModel):
    revision: int = Field(ge=1)
    catalog_object_id: str | None = Field(default=None, max_length=255)
    captured_at: datetime | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_name: str | None = Field(default=None, max_length=255)
    equipment_id: str | None = Field(default=None, max_length=255)
    memo: str | None = None
    favorite: bool | None = None
    representative: bool | None = None
    plate_solve_status: str | None = Field(default=None, max_length=30)


class ObservationRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    file_id: int
    service_name: str
    catalog_object_id: str | None
    captured_at: datetime
    latitude: float | None
    longitude: float | None
    location_name: str | None
    equipment_id: str | None
    memo: str | None
    favorite: bool
    representative: bool
    plate_solve_status: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    revision: int
