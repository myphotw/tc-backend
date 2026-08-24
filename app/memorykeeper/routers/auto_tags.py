from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.memorykeeper.schemas.auto_tag import (
    AutoTagCurationPreviewResponse,
    AutoTagFailedJobListResponse,
    AutoTagRetryResponse,
    AutoTagStatusResponse,
)
from app.memorykeeper.services.auto_tag_service import MemoryKeeperAutoTagService


router = APIRouter(
    prefix="/api/memorykeeper/auto-tags",
    tags=["MemoryKeeper Auto Tags"],
)


@router.get("/status", response_model=AutoTagStatusResponse)
def get_auto_tag_status(db: Session = Depends(get_db)) -> AutoTagStatusResponse:
    return MemoryKeeperAutoTagService(db).status()


@router.get("/failed", response_model=AutoTagFailedJobListResponse)
def list_failed_auto_tag_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> AutoTagFailedJobListResponse:
    return MemoryKeeperAutoTagService(db).failed_jobs(
        page=page,
        page_size=page_size,
    )


@router.post("/retry-failed", response_model=AutoTagRetryResponse)
def retry_failed_auto_tag_jobs(
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> AutoTagRetryResponse:
    return MemoryKeeperAutoTagService(db).retry_failed(limit=limit)


@router.post("/jobs/{job_id}/retry", response_model=AutoTagRetryResponse)
def retry_auto_tag_job(
    job_id: int,
    db: Session = Depends(get_db),
) -> AutoTagRetryResponse:
    return MemoryKeeperAutoTagService(db).retry_job(job_id)


@router.get("/curation-preview", response_model=AutoTagCurationPreviewResponse)
def preview_auto_tag_curation(
    sample_limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> AutoTagCurationPreviewResponse:
    return MemoryKeeperAutoTagService(db).curation_preview(
        sample_limit=sample_limit,
    )
