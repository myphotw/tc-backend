"""Gallery Query API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GalleryListItem(BaseModel):
    """Gallery 목록 항목."""

    model_config = ConfigDict(from_attributes=True)

    file_id: str
    filename: str
    preview_url: str | None = None
    thumbnail_url: str | None = None
    capture_datetime: datetime | None = None
    country: str | None = None
    province: str | None = None
    city: str | None = None
    district: str | None = None
    place_name: str | None = None
    geocoded_place_name: str | None = None
    memorykeeper_place_id: str | None = None
    place_display_name: str | None = None
    place_canonical_name: str | None = None
    place_match_source: str | None = None
    place_match_distance_m: float | None = None
    place_revision: int | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None
    camera_model: str | None = None
    favorite: bool = False
    memo: str | None = None
    metadata_revision: int = 0
    incomplete: bool = False
    has_gps: bool = False
    has_ai_tag: bool = False
    service_name: str = "MemoryKeeper"


class GalleryListResponse(BaseModel):
    """Gallery 목록 응답."""

    items: list[GalleryListItem]
    page: int
    page_size: int
    total: int
    sort: str


class GalleryTagItem(BaseModel):
    """Gallery Tag 항목."""

    tag: str
    source: str
    tag_type: str
    confidence: float | None = None
    tag_id: int | None = None


class GalleryDetailResponse(BaseModel):
    """Gallery 상세 응답."""

    file_id: str
    filename: str
    extension: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    favorite: bool = False
    memo: str | None = None
    metadata_revision: int = 0
    incomplete: bool = False
    service_name: str = "MemoryKeeper"
    memorykeeper_place_id: str | None = None
    place_display_name: str | None = None
    place_canonical_name: str | None = None
    geocoded_place_name: str | None = None
    place_match_source: str | None = None
    place_match_distance_m: float | None = None
    place_revision: int | None = None
    storage_path: str | None = None
    preview_url: str | None = None
    thumbnail_url: str | None = None
    original_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    ai_tags: list[GalleryTagItem] = Field(default_factory=list)
    user_tags: list[GalleryTagItem] = Field(default_factory=list)
    history_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class GallerySearchResponse(GalleryListResponse):
    """Gallery 검색 응답."""


class MapMarkerResponse(BaseModel):
    """지도 Marker 항목."""

    file_id: str
    latitude: float
    longitude: float
    place_name: str | None = None
    geocoded_place_name: str | None = None
    memorykeeper_place_id: str | None = None
    place_display_name: str | None = None
    place_canonical_name: str | None = None
    place_match_source: str | None = None
    place_match_distance_m: float | None = None
    place_revision: int | None = None
    province: str | None = None
    district: str | None = None
    thumbnail: str | None = None
    year: int | None = None
    service_name: str = "MemoryKeeper"


class MapMarkerListResponse(BaseModel):
    """지도 Marker 목록 응답."""

    items: list[MapMarkerResponse]
    total: int


class TimelineItem(BaseModel):
    """Timeline 년도 그룹."""

    year: int
    count: int


class TimelineResponse(BaseModel):
    """Timeline 응답."""

    items: list[TimelineItem]
    total: int


class CountItem(BaseModel):
    """이름/건수 통계 항목."""

    name: str
    count: int


class StatisticsResponse(BaseModel):
    """Gallery 통계 응답."""

    total_photos: int
    gps_count: int
    ai_tag_count: int
    by_camera: list[CountItem] = Field(default_factory=list)
    by_country: list[CountItem] = Field(default_factory=list)
    by_year: list[CountItem] = Field(default_factory=list)
    by_service: list[CountItem] = Field(default_factory=list)
