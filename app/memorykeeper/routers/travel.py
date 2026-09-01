"""MemoryKeeper-only TravelRecords fast-read endpoints."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.memorykeeper.schemas.fast_travel import (
    FastTravelAggregatesResponse,
    FastTravelMemoriesResponse,
)
from app.memorykeeper.services.fast_travel_service import MemoryKeeperFastTravelService


router = APIRouter(prefix="/api/memorykeeper/travel", tags=["MemoryKeeper Travel"])


@router.get("/aggregates", response_model=FastTravelAggregatesResponse)
def travel_aggregates(
    db: Session = Depends(get_db),
) -> FastTravelAggregatesResponse:
    """Return place/country visits without materializing the photo catalog."""
    return MemoryKeeperFastTravelService(db).aggregates()


@router.get("/memories", response_model=FastTravelMemoriesResponse)
def travel_memories(
    reference_date: date = Query(
        ...,
        description="Canonical calendar date used to select past memories",
    ),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> FastTravelMemoriesResponse:
    """Return bounded anniversary/previous-year candidates for one date."""
    return MemoryKeeperFastTravelService(db).memories(
        reference_date=reference_date,
        limit=limit,
    )

