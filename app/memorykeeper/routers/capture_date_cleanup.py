"""MemoryKeeper capture-date cleanup group endpoints."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.memorykeeper.schemas.cleanup import (
    CaptureDateCleanupGroupPage,
    CaptureDateCleanupPhotoPage,
)
from app.memorykeeper.services.cleanup_group_service import MemoryKeeperCleanupGroupService


router = APIRouter(
    prefix="/api/memorykeeper/capture-date-cleanup",
    tags=["MemoryKeeper Capture Date Cleanup"],
)


@router.get("/groups", response_model=CaptureDateCleanupGroupPage)
def list_capture_date_cleanup_groups(
    limit: int = Query(5, ge=1, le=50),
    cursor: str | None = Query(None, description="Opaque group keyset cursor"),
    db: Session = Depends(get_db),
) -> CaptureDateCleanupGroupPage:
    return MemoryKeeperCleanupGroupService(db).date_groups(limit=limit, cursor=cursor)


@router.get("/groups/{group_id}/photos", response_model=CaptureDateCleanupPhotoPage)
def list_capture_date_cleanup_group_photos(
    group_id: str,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, description="Opaque photo keyset cursor"),
    db: Session = Depends(get_db),
) -> CaptureDateCleanupPhotoPage:
    return MemoryKeeperCleanupGroupService(db).date_group_photos(
        group_id=group_id,
        limit=limit,
        cursor=cursor,
    )
