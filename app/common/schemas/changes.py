from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChangeEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cursor: int = Field(validation_alias="id")
    service_name: str
    resource_type: str
    resource_id: str
    operation: str
    revision: int | None
    tombstone: bool
    changed_at: datetime


class ChangesResponse(BaseModel):
    items: list[ChangeEventResponse]
    next_cursor: int
    has_more: bool
