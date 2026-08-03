"""공통 파일 업로드 API."""

from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.common.models.upload_job import UploadJob
from app.common.repositories.upload_job_repository import UploadJobRepository
from app.common.services.storage_service import StorageService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/common",
    tags=["Upload"],
)

storage_service = StorageService()


@router.post("/upload")
def upload_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict[str, str | int]:
    """
    파일을 incoming 영역에 저장하고 업로드 작업을 생성한다.

    후처리(SHA256, 중복 검사, preview/thumbnail 생성, common_files 등록)는
    background_worker.py가 담당한다.

    Args:
        file: 업로드 파일
        db: SQLAlchemy 세션

    Returns:
        dict[str, str | int]: 생성된 업로드 작업 정보
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="filename is required")

    incoming_path: str | None = None
    try:
        job_id = str(uuid4())
        incoming_path = storage_service.save_incoming(file, job_id)
        repository = UploadJobRepository(db)
        job: UploadJob = repository.create_waiting_job(
            job_id=job_id,
            source_type="UPLOAD",
            incoming_path=incoming_path,
        )

        logger.info(
            "Created upload job id=%s job_id=%s incoming_path=%s",
            job.id,
            job.job_id,
            job.incoming_path,
        )
        return {
            "id": job.id,
            "job_id": job.job_id,
            "status": job.status,
            "incoming_path": job.incoming_path,
        }

    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        if incoming_path is not None:
            storage_service.delete_incoming(incoming_path)
        logger.exception("Upload failed: filename=%s", file.filename)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload file: {exc}",
        ) from exc
