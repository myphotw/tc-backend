from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CatalogFavoriteCreate(BaseModel):
    catalog_object_id: str = Field(min_length=1, max_length=255)

    @field_validator("catalog_object_id")
    @classmethod
    def normalize_catalog_object_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("catalog_object_id cannot be blank")
        return normalized


class CatalogFavoriteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    catalog_object_id: str
    created_at: datetime
    updated_at: datetime


class CatalogFavoriteDeleteResponse(BaseModel):
    catalog_object_id: str
    deleted: bool
