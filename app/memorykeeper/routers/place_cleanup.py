"""MemoryKeeper place-cleanup work-list endpoint."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.memorykeeper.schemas.cleanup import (
    PlaceCleanupGroupPage,
    PlaceCleanupPhotoPage,
)
from app.memorykeeper.schemas.pending import PendingListResponse
from app.memorykeeper.services.cleanup_group_service import (
    MemoryKeeperCleanupGroupService,
)
from app.memorykeeper.services.place_cleanup_service import (
    MemoryKeeperPlaceCleanupService,
)


router = APIRouter(
    prefix="/api/memorykeeper/place-cleanup",
    tags=["MemoryKeeper Place Cleanup"],
)


@router.get("/groups", response_model=PlaceCleanupGroupPage)
def list_place_cleanup_groups(
    limit: int = Query(5, ge=1, le=50),
    cursor: str | None = Query(None, description="Opaque group keyset cursor"),
    db: Session = Depends(get_db),
) -> PlaceCleanupGroupPage:
    return MemoryKeeperCleanupGroupService(db).place_groups(limit=limit, cursor=cursor)


@router.get("/groups/{group_id}/photos", response_model=PlaceCleanupPhotoPage)
def list_place_cleanup_group_photos(
    group_id: str,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, description="Opaque photo keyset cursor"),
    db: Session = Depends(get_db),
) -> PlaceCleanupPhotoPage:
    return MemoryKeeperCleanupGroupService(db).place_group_photos(
        group_id=group_id,
        limit=limit,
        cursor=cursor,
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
