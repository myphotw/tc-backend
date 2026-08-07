from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AstroGalleryItem(BaseModel):
    record_id: UUID
    revision: int
    catalog_object_id: str | None = None
    captured_at: datetime
    latitude: float | None = None
    longitude: float | None = None
    location_name: str | None = None
    memo: str | None = None
    favorite: bool
    representative: bool
    file_id: str
    filename: str
    mime_type: str | None = None
    thumbnail_url: str | None = None
    preview_url: str | None = None
    original_url: str | None = None
    capture_datetime: datetime | None = None


class AstroGalleryListResponse(BaseModel):
    items: list[AstroGalleryItem]
    page: int
    page_size: int
    total: int
