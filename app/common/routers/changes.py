from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.common.schemas.changes import ChangesResponse
from app.common.services.changes_service import ChangesService

router = APIRouter(prefix="/api/common", tags=["Changes"])


@router.get("/changes", response_model=ChangesResponse)
def list_changes(
    cursor: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    service_name: Literal["MemoryKeeper", "AstroJournal"] | None = Query(None),
    db: Session = Depends(get_db),
) -> ChangesResponse:
    return ChangesService(db).list_changes(
        cursor=cursor,
        limit=limit,
        service_name=service_name,
    )
