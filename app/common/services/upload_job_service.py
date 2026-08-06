"""Upload Job Status Service."""

from __future__ import annotations

import re

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.common.models.upload_job import UploadJob
from app.common.repositories.upload_job_repository import (
    UploadJobRepository,
    UploadJobStatus,
)
from app.common.schemas.upload_job import (
    UploadJobListResponse,
    UploadJobStatusResponse,
)


class UploadJobService:
    """Upload Job 조회 Business Logic."""

    PLUGIN_ORDER = (
        "HashPlugin",
        "PreviewPlugin",
        "StoragePlugin",
        "MetadataPlugin",
        "ExifPlugin",
        "GpsPlugin",
    )
    PROGRESS_BY_COMPLETED = {
        0: 0,
        1: 16,
        2: 33,
        3: 50,
        4: 66,
        5: 83,
        6: 100,
    }

    def __init__(self, db: Session) -> None:
        self.repository = UploadJobRepository(db)

    def list_jobs(
        self,
        *,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: str = "created_at_desc",
    ) -> UploadJobListResponse:
        if status is not None:
            allowed = {
                UploadJobStatus.WAITING,
                UploadJobStatus.PROCESSING,
                UploadJobStatus.FAILED,
                UploadJobStatus.COMPLETED,
            }
            if status not in allowed:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "status must be one of WAITING, PROCESSING, FAILED, COMPLETED"
                    ),
                )

        jobs, total = self.repository.list(
            status=status,
            page=page,
            page_size=page_size,
            sort=sort,
        )
        return UploadJobListResponse(
            items=[self._to_response(job) for job in jobs],
            page=page,
            page_size=page_size,
            total=total,
            sort=sort,
        )

    def get_job(self, job_id: str) -> UploadJobStatusResponse:
        job = self.repository.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Upload job not found")
        return self._to_response(job)

    def _to_response(self, job: UploadJob) -> UploadJobStatusResponse:
        log_text = job.processing_log or ""
        return UploadJobStatusResponse(
            job_id=job.job_id,
            status=job.status,
            progress=self._calculate_progress(job),
            current_plugin=self._extract_current_plugin(log_text),
            processing_log=log_text or None,
            retry_count=job.retry_count or 0,
            requested_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            last_error=job.error_message,
            updated_at=job.completed_at or job.started_at or job.created_at,
            service_name=job.service_name,
            client_file_id=job.client_file_id,
            backend_file_id=job.file_id,
        )

    def _calculate_progress(self, job: UploadJob) -> int:
        if job.status == UploadJobStatus.COMPLETED:
            return 100
        if job.status == UploadJobStatus.WAITING:
            return 0

        completed = self._count_completed_plugins(job.processing_log or "")
        if job.status == UploadJobStatus.FAILED:
            return self.PROGRESS_BY_COMPLETED.get(completed, 0)
        return self.PROGRESS_BY_COMPLETED.get(min(completed, 6), 0)

    def _count_completed_plugins(self, processing_log: str) -> int:
        lines = self._split_log_lines(processing_log)
        completed = 0
        for plugin_name in self.PLUGIN_ORDER:
            marker = f"PLUGIN_COMPLETE {plugin_name}"
            if any(marker in line for line in lines):
                completed += 1
            else:
                break
        return completed

    def _extract_current_plugin(self, processing_log: str) -> str | None:
        lines = self._split_log_lines(processing_log)
        for line in reversed(lines):
            match = re.search(r"PLUGIN_START\s+(\S+)", line)
            if match:
                return match.group(1)
        return None

    @staticmethod
    def _split_log_lines(processing_log: str) -> list[str]:
        # Repository가 과거부터 "\\n" 리터럴을 저장하는 경우를 모두 지원한다.
        normalized = processing_log.replace("\\n", "\n")
        return [line.strip() for line in normalized.splitlines() if line.strip()]
