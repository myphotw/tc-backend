"""Upload Job Status API Router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.common.schemas.upload_job import (
    UploadJobListResponse,
    UploadJobStatusResponse,
)
from app.common.services.upload_job_service import UploadJobService

router = APIRouter(
    prefix="/api/common/upload/jobs",
    tags=["Upload Jobs"],
)


@router.get(
    "",
    response_model=UploadJobListResponse,
    summary="List upload jobs",
    description=(
        "WAITING / PROCESSING / FAILED / COMPLETED UploadJob 목록을 조회한다."
    ),
)
def list_upload_jobs(
    status: str | None = Query(
        None,
        description="WAITING | PROCESSING | FAILED | COMPLETED",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    sort: str = Query("created_at_desc"),
    db: Session = Depends(get_db),
) -> UploadJobListResponse:
    """Upload Job 목록을 조회한다."""
    return UploadJobService(db).list_jobs(
        status=status,
        page=page,
        page_size=page_size,
        sort=sort,
    )


@router.get(
    "/{job_id}",
    response_model=UploadJobStatusResponse,
    summary="Get upload job",
    description="단일 Upload Job 상태를 조회한다.",
    responses={404: {"description": "Upload job not found"}},
)
def get_upload_job(
    job_id: str,
    db: Session = Depends(get_db),
) -> UploadJobStatusResponse:
    """단일 Upload Job을 조회한다."""
    return UploadJobService(db).get_job(job_id)
