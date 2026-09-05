"""Lightweight, MemoryKeeper-only Gallery read contracts."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class FastGalleryPhotoItem(BaseModel):
    """The card-sized projection returned by keyset Gallery reads."""

    common_file_id: int
    file_id: str
    filename: str
    extension: str | None = None
    mime_type: str | None = None
    preview_url: str | None = None
    thumbnail_url: str | None = None
    favorite: bool
    has_gps: bool
    effective_capture_datetime: datetime
    effective_capture_date: date
    effective_capture_year: int
    date_basis: str | None = None
    memorykeeper_place_id: str | None = None
    place_display_name: str | None = None
    country: str | None = None
    region: str | None = None


class FastGalleryPhotosResponse(BaseModel):
    items: list[FastGalleryPhotoItem] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool
    # The existing common change feed does not yet guarantee CREATE events for
    # all uploads, so this intentionally is not advertised as a delta cursor.
    sync_cursor: int | None = None


class FastGalleryCount(BaseModel):
    name: str | None
    count: int


class FastGallerySummaryResponse(BaseModel):
    total_photos: int
    favorite_count: int
    recent_count: int
    pending_count: int
    gps_count: int
    effective_date_min: date | None = None
    effective_date_max: date | None = None
    by_year: list[FastGalleryCount] = Field(default_factory=list)
    by_country: list[FastGalleryCount] = Field(default_factory=list)


class FastGalleryPlaceNode(BaseModel):
    memorykeeper_place_id: str | None = None
    display_name: str | None = None
    count: int


class FastGalleryRegionNode(BaseModel):
    region: str | None = None
    count: int
    places: list[FastGalleryPlaceNode] = Field(default_factory=list)


class FastGalleryCountryNode(BaseModel):
    country: str | None = None
    count: int
    regions: list[FastGalleryRegionNode] = Field(default_factory=list)


class FastGalleryYearNode(BaseModel):
    year: int
    count: int
    countries: list[FastGalleryCountryNode] = Field(default_factory=list)


class FastGalleryHierarchyResponse(BaseModel):
    items: list[FastGalleryYearNode] = Field(default_factory=list)
