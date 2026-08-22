from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.memorykeeper.schemas.pending import (
    PendingAssignPlaceRequest,
    PendingAssignPlaceResponse,
    PendingListResponse,
)
from app.memorykeeper.services.pending_service import MemoryKeeperPendingService


router = APIRouter(prefix="/api/memorykeeper/pending", tags=["MemoryKeeper Pending"])


@router.get("", response_model=PendingListResponse)
def list_pending(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    include_suggestions: bool = Query(False),
    db: Session = Depends(get_db),
) -> PendingListResponse:
    return MemoryKeeperPendingService(db).list(
        page=page,
        page_size=page_size,
        include_suggestions=include_suggestions,
    )


@router.post("/assign-place", response_model=PendingAssignPlaceResponse)
def assign_pending_place(
    payload: PendingAssignPlaceRequest,
    db: Session = Depends(get_db),
) -> PendingAssignPlaceResponse:
    return MemoryKeeperPendingService(db).assign_place(payload)
