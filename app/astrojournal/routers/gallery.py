from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.astrojournal.schemas.gallery import AstroGalleryItem, AstroGalleryListResponse
from app.astrojournal.services.gallery_service import AstroGalleryService
from app.common.database import get_db

router = APIRouter(prefix="/api/astro/gallery", tags=["AstroJournal Gallery"])


@router.get("", response_model=AstroGalleryListResponse)
def list_gallery(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    catalog_object_id: str | None = Query(None),
    favorite: bool | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    db: Session = Depends(get_db),
) -> AstroGalleryListResponse:
    return AstroGalleryService(db).list_gallery(
        page=page,
        page_size=page_size,
        catalog_object_id=catalog_object_id,
        favorite=favorite,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/{record_id}", response_model=AstroGalleryItem)
def get_gallery_record(
    record_id: str,
    db: Session = Depends(get_db),
) -> AstroGalleryItem:
    return AstroGalleryService(db).get_detail(record_id)
