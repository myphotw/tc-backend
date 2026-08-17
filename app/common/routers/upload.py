"""공통 파일 업로드 API."""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.common.models.upload_job import UploadJob
from app.common.repositories.upload_job_repository import UploadJobRepository
from app.common.services.storage_service import StorageService
from app.common.services.upload_metadata import encode_upload_metadata
from app.common.utils.perf import Stopwatch, log_perf

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/common",
    tags=["Upload"],
)

storage_service = StorageService()
SUPPORTED_SERVICES = {"MemoryKeeper", "AstroJournal"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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
    service_name: str = Form("MemoryKeeper"),
    client_file_id: str | None = Form(None),
    client_content_sha256: str | None = Form(None),
    observation_date: Annotated[date | None, Form()] = None,
    canonical_target_id: Annotated[str | None, Form()] = None,
    target_display_name: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
) -> dict[str, str | int | bool | None]:
    """
    파일을 incoming 영역에 저장하고 업로드 작업을 생성한다.

    후처리(SHA256, 중복 검사, preview/thumbnail 생성, common_files 등록)는
    background_worker.py가 담당한다.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="filename is required")

    service_name = _validate_service_name(service_name)
    client_file_id = _normalize_client_file_id(client_file_id)
    client_content_sha256 = _normalize_client_content_sha256(client_content_sha256)
    canonical_target_id = _normalize_optional_metadata(
        canonical_target_id,
        field_name="canonical_target_id",
    )
    target_display_name = _normalize_optional_metadata(
        target_display_name,
        field_name="target_display_name",
    )
    upload_metadata_log = encode_upload_metadata(
        {
            "observation_date": observation_date,
            "canonical_target_id": canonical_target_id,
            "target_display_name": target_display_name,
        }
    )
    repository = UploadJobRepository(db)
    if client_file_id is not None:
        existing = repository.get_by_client_file_id(
            service_name=service_name,
            client_file_id=client_file_id,
        )
        if existing is not None:
            return _idempotent_response(repository, existing, client_content_sha256)

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
        try:
            job: UploadJob = repository.create_waiting_job(
                job_id=job_id,
                source_type="UPLOAD",
                incoming_path=incoming_path,
                service_name=service_name,
                client_file_id=client_file_id,
                client_content_sha256=client_content_sha256,
                processing_log=upload_metadata_log,
            )
        except IntegrityError:
            db.rollback()
            storage_service.delete_incoming(incoming_path)
            if client_file_id is None:
                raise
            existing = repository.get_by_client_file_id(
                service_name=service_name,
                client_file_id=client_file_id,
            )
            if existing is None:
                raise
            return _idempotent_response(repository, existing, client_content_sha256)
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
        response: dict[str, str | int | bool | None] = {
            "id": job.id,
            "job_id": job.job_id,
            "status": job.status,
            "incoming_path": job.incoming_path,
        }
        if client_file_id is not None or service_name != "MemoryKeeper":
            response.update(
                {
                    "service_name": job.service_name,
                    "client_file_id": job.client_file_id,
                    "backend_file_id": job.file_id,
                    "common_file_id": repository.resolve_common_file_id(job.file_id),
                    "idempotent_replay": False,
                }
            )
        return response

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


def _validate_service_name(value: str) -> str:
    normalized = (value or "").strip()
    if normalized not in SUPPORTED_SERVICES:
        raise HTTPException(
            status_code=422,
            detail="service_name must be MemoryKeeper or AstroJournal",
        )
    return normalized


def _normalize_client_file_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        raise HTTPException(
            status_code=422,
            detail="client_file_id must be a non-empty string up to 255 characters",
        )
    return normalized


def _normalize_client_content_sha256(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if not _SHA256_RE.fullmatch(value):
        raise HTTPException(
            status_code=422,
            detail="client_content_sha256 must be a 64-character lowercase hex SHA-256",
        )
    return value


def _normalize_optional_metadata(
    value: str | None,
    *,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 255:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be up to 255 characters",
        )
    return normalized


def _idempotent_response(
    repository: UploadJobRepository,
    job: UploadJob,
    requested_hash: str | None,
) -> dict[str, str | int | bool | None]:
    existing_hash = job.client_content_sha256
    if existing_hash and requested_hash and existing_hash != requested_hash:
        raise HTTPException(
            status_code=409,
            detail="client_file_id is already associated with a different content hash",
        )
    if requested_hash and not existing_hash:
        job = repository.set_client_content_sha256_if_missing(
            job,
            client_content_sha256=requested_hash,
        )
    return {
        "id": job.id,
        "job_id": job.job_id,
        "status": job.status,
        "incoming_path": job.incoming_path,
        "service_name": job.service_name,
        "client_file_id": job.client_file_id,
        "backend_file_id": job.file_id,
        "common_file_id": repository.resolve_common_file_id(job.file_id),
        "idempotent_replay": True,
    }
