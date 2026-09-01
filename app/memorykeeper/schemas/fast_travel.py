"""Lightweight MemoryKeeper TravelRecords read contracts."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class FastTravelPlaceAggregate(BaseModel):
    memorykeeper_place_id: str | None = None
    place_display_name: str | None = None
    country: str | None = None
    region: str | None = None
    photo_count: int
    capture_dates: list[date] = Field(default_factory=list)
    visit_count: int
    representative_common_file_id: int | None = None
    representative_file_id: str | None = None
    representative_capture_date: date | None = None
    representative_preview_url: str | None = None
    representative_thumbnail_url: str | None = None


class FastTravelCountryAggregate(BaseModel):
    country: str | None = None
    photo_count: int
    capture_dates: list[date] = Field(default_factory=list)
    visit_count: int
    representative_common_file_id: int | None = None
    representative_file_id: str | None = None
    representative_capture_date: date | None = None
    representative_preview_url: str | None = None
    representative_thumbnail_url: str | None = None


class FastTravelAggregatesResponse(BaseModel):
    places: list[FastTravelPlaceAggregate] = Field(default_factory=list)
    countries: list[FastTravelCountryAggregate] = Field(default_factory=list)


class FastTravelMemoryCandidate(BaseModel):
    common_file_id: int
    file_id: str
    effective_capture_date: date
    effective_capture_year: int
    years_ago: int
    day_offset: int
    memorykeeper_place_id: str | None = None
    place_display_name: str | None = None
    country: str | None = None
    preview_url: str | None = None
    thumbnail_url: str | None = None


class FastTravelMemoriesResponse(BaseModel):
    reference_date: date
    exact_anniversary: list[FastTravelMemoryCandidate] = Field(default_factory=list)
    previous_year_period: list[FastTravelMemoryCandidate] = Field(default_factory=list)
