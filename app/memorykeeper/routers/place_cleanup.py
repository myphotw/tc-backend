"""MemoryKeeper place-cleanup work-list endpoint."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.memorykeeper.schemas.pending import PendingListResponse
from app.memorykeeper.services.place_cleanup_service import (
    MemoryKeeperPlaceCleanupService,
)


router = APIRouter(
    prefix="/api/memorykeeper/place-cleanup",
    tags=["MemoryKeeper Place Cleanup"],
)


@router.get("", response_model=PendingListResponse)
def list_place_cleanup(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> PendingListResponse:
    return MemoryKeeperPlaceCleanupService(db).list(
        page=page,
        page_size=page_size,
    )
