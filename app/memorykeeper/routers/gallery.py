"""MemoryKeeper-only fast Gallery read endpoints."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.memorykeeper.repositories.fast_gallery_repository import FastGalleryFilters
from app.memorykeeper.schemas.fast_gallery import (
    FastGalleryHierarchyResponse,
    FastGalleryPhotosResponse,
    FastGallerySummaryResponse,
)
from app.memorykeeper.services.fast_gallery_service import MemoryKeeperFastGalleryService


router = APIRouter(prefix="/api/memorykeeper/gallery", tags=["MemoryKeeper Gallery"])


@router.get("/photos", response_model=FastGalleryPhotosResponse)
def list_photos(
    cursor: str | None = Query(None, description="Opaque keyset cursor"),
    limit: int = Query(50, ge=1, le=100),
    year: int | None = Query(None, ge=1),
    country: str | None = Query(None, max_length=100),
    region: str | None = Query(None, max_length=100),
    place_id: str | None = Query(None, max_length=36),
    favorite: bool | None = Query(None),
    has_gps: bool | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    db: Session = Depends(get_db),
) -> FastGalleryPhotosResponse:
    """Return one lightweight card page without an OFFSET or total COUNT."""
    return MemoryKeeperFastGalleryService(db).photos(
        cursor=cursor,
        limit=limit,
        filters=FastGalleryFilters(
            year=year,
            country=country,
            region=region,
            place_id=place_id,
            favorite=favorite,
            has_gps=has_gps,
            date_from=date_from,
            date_to=date_to,
        ),
    )


@router.get("/summary", response_model=FastGallerySummaryResponse)
def gallery_summary(db: Session = Depends(get_db)) -> FastGallerySummaryResponse:
    """Return set-based MemoryKeeper card/statistics aggregates."""
    return MemoryKeeperFastGalleryService(db).summary()


@router.get("/hierarchy", response_model=FastGalleryHierarchyResponse)
def gallery_hierarchy(db: Session = Depends(get_db)) -> FastGalleryHierarchyResponse:
    """Return year -> country -> region -> place aggregate nodes."""
    return MemoryKeeperFastGalleryService(db).hierarchy()
