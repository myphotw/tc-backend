"""Normalized AstroJournal astronomy event contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


AstronomyEventType = Literal[
    "meteor_shower",
    "solar_eclipse",
    "lunar_eclipse",
    "planet_viewing",
    "conjunction",
]


class AstronomyEventItem(BaseModel):
    id: str
    type: AstronomyEventType
    title: str
    start_at: datetime | None = None
    peak_at: datetime
    end_at: datetime | None = None
    tags: list[str] = Field(max_length=2)
    priority: int


class AstronomyEventListResponse(BaseModel):
    events: list[AstronomyEventItem]
