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
from app.common.utils.perf import Stopwatch, log_perf

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/common",
    tags=["Upload"],
)

storage_service = StorageService()


@router.post(
    "/upload",
    summary="Upload file",
    description=(
        "Multipart 파일을 incoming에 저장하고 UploadJob(WAITING)을 생성한다. "
        "후처리는 UploadWorker가 담당한다."
    ),
    response_description="생성된 UploadJob 정보",
    responses={
        200: {
            "description": "UploadJob 생성 성공",
            "content": {
                "application/json": {
                    "example": {
                        "id": 1,
                        "job_id": "uuid",
                        "status": "WAITING",
                        "incoming_path": "incoming/uuid_file.jpg",
                    }
                }
            },
        },
        400: {"description": "filename 누락"},
        500: {"description": "업로드 실패"},
    },
)
def upload_file(
    file: UploadFile = File(..., description="업로드할 원본 파일"),
    db: Session = Depends(get_db),
) -> dict[str, str | int]:
    """
    파일을 incoming 영역에 저장하고 업로드 작업을 생성한다.

    후처리(SHA256, 중복 검사, preview/thumbnail 생성, common_files 등록)는
    background_worker.py가 담당한다.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="filename is required")

    watch = Stopwatch()
    incoming_path: str | None = None
    file_size: int | None = None
    try:
        job_id = str(uuid4())
        # multipart 수신은 UploadFile 생성 시점까지 포함되므로
        # 저장 직전 size 확인 + save_incoming 구간을 분리 계측한다.
        watch.start("multipart_receive")
        # FastAPI가 이미 파싱한 스트림 크기 추정 (가능 시)
        try:
            pos = file.file.tell()
            file.file.seek(0, 2)
            file_size = file.file.tell()
            file.file.seek(pos)
        except Exception:
            file_size = None
        multipart_ms = watch.stop("multipart_receive")

        watch.start("incoming_save")
        incoming_path = storage_service.save_incoming(file, job_id)
        incoming_ms = watch.stop("incoming_save")

        watch.start("upload_job_create")
        repository = UploadJobRepository(db)
        job: UploadJob = repository.create_waiting_job(
            job_id=job_id,
            source_type="UPLOAD",
            incoming_path=incoming_path,
        )
        job_ms = watch.stop("upload_job_create")

        log_perf(
            "upload_api",
            multipart_receive_ms=multipart_ms,
            incoming_save_ms=incoming_ms,
            upload_job_create_ms=job_ms,
            file_size=file_size,
            elapsed_ms=watch.total_ms(),
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
