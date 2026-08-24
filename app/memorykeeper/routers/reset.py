from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.memorykeeper.schemas.reset import (
    MemoryKeeperResetExecuteRequest,
    MemoryKeeperResetExecuteResponse,
    MemoryKeeperResetPreviewResponse,
)
from app.memorykeeper.services.reset_service import MemoryKeeperResetService


router = APIRouter(
    prefix="/api/memorykeeper/reset",
    tags=["MemoryKeeper Reset"],
)


@router.post("/preview", response_model=MemoryKeeperResetPreviewResponse)
def preview_memorykeeper_reset(
    db: Session = Depends(get_db),
) -> MemoryKeeperResetPreviewResponse:
    """Return the semantic reset impact without mutating database state."""
    return MemoryKeeperResetService(db).preview()


@router.post("/execute", response_model=MemoryKeeperResetExecuteResponse)
def execute_memorykeeper_reset(
    payload: MemoryKeeperResetExecuteRequest,
    db: Session = Depends(get_db),
) -> MemoryKeeperResetExecuteResponse:
    """Reset MemoryKeeper semantics while preserving shared physical assets."""
    _ = payload  # Literal validation is the explicit confirmation boundary.
    return MemoryKeeperResetService(db).execute()
