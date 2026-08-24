from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    favorite: bool = False

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = " ".join(value.strip().split())
        if not value:
            raise ValueError("name cannot be blank")
        return value


class TagUpdate(BaseModel):
    revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    favorite: bool | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.strip().split())
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_update(self) -> "TagUpdate":
        if not (self.model_fields_set - {"revision"}):
            raise ValueError("at least one mutable field is required")
        if "favorite" in self.model_fields_set and self.favorite is None:
            raise ValueError("favorite cannot be null")
        return self


class TagMergeRequest(BaseModel):
    source_revision: int = Field(ge=1)
    target_tag_id: int = Field(gt=0)
    target_revision: int = Field(ge=1)


class TagResponse(BaseModel):
    id: int
    name: str
    tag_type: str
    source: str
    favorite: bool
    usage_count: int
    revision: int
    created_at: datetime | None
    updated_at: datetime | None


class TagListResponse(BaseModel):
    items: list[TagResponse]
    total: int


class UnifiedTagCatalogItem(BaseModel):
    identity: str
    display_name: str
    usage_count: int
    favorite: bool = False
    revision: int
    editable: bool = True
    canonical_references: list[str] = Field(default_factory=list)


class UnifiedTagCatalogResponse(BaseModel):
    items: list[UnifiedTagCatalogItem]
    total: int


class UnifiedTagRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = " ".join(value.strip().split())
        if not value:
            raise ValueError("name cannot be blank")
        return value
