from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.astrojournal.schemas.reset import (
    AstroJournalResetExecuteRequest,
    AstroJournalResetExecuteResponse,
    AstroJournalResetPreviewResponse,
)
from app.astrojournal.services.reset_service import AstroJournalResetService
from app.common.database import get_db


router = APIRouter(prefix="/api/astro/reset", tags=["AstroJournal Reset"])


@router.post("/preview", response_model=AstroJournalResetPreviewResponse)
def preview_astrojournal_reset(
    db: Session = Depends(get_db),
) -> AstroJournalResetPreviewResponse:
    """Return the capture-data reset impact without mutation."""
    return AstroJournalResetService(db).preview()


@router.post("/execute", response_model=AstroJournalResetExecuteResponse)
def execute_astrojournal_reset(
    payload: AstroJournalResetExecuteRequest,
    db: Session = Depends(get_db),
) -> AstroJournalResetExecuteResponse:
    """Remove AstroJournal capture data under the ownership safety policy."""
    _ = payload
    return AstroJournalResetService(db).execute()
