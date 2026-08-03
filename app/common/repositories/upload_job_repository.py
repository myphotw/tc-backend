from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.common.models.upload_job import UploadJob


class UploadJobStatus:
    """업로드 작업 상태 값."""

    WAITING = "WAITING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class UploadJobRepository:
    """common_upload_jobs 저장소."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create_waiting_job(
        self,
        *,
        job_id: str,
        source_type: str,
        incoming_path: str,
    ) -> UploadJob:
        """WAITING 상태의 업로드 작업을 생성한다."""
        job = UploadJob(
            job_id=job_id,
            source_type=source_type,
            status=UploadJobStatus.WAITING,
            incoming_path=incoming_path,
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def get_next_waiting_job(self) -> UploadJob | None:
        """가장 오래된 WAITING 작업을 조회한다."""
        return (
            self.db.query(UploadJob)
            .filter(UploadJob.status == UploadJobStatus.WAITING)
            .order_by(UploadJob.created_at.asc(), UploadJob.id.asc())
            .first()
        )

    def mark_processing(self, job: UploadJob) -> UploadJob:
        """작업을 PROCESSING 상태로 변경한다."""
        job.status = UploadJobStatus.PROCESSING
        job.started_at = datetime.now(timezone.utc)
        job.processing_log = (job.processing_log or "") + ("START\\n")
        job.error_message = None
        self.db.commit()
        self.db.refresh(job)
        return job

    def mark_completed(self, job: UploadJob, *, file_id: str) -> UploadJob:
        """작업을 COMPLETED 상태로 변경한다."""
        job.status = UploadJobStatus.COMPLETED
        job.file_id = file_id
        job.completed_at = datetime.now(timezone.utc)
        job.error_message = None
        job.processing_log = (job.processing_log or "") + ("COMPLETED\\n")
        self.db.commit()
        self.db.refresh(job)
        return job

    def mark_failed(self, job: UploadJob, *, error_message: str) -> UploadJob:
        """작업을 FAILED 상태로 변경한다."""
        job.status = UploadJobStatus.FAILED
        job.retry_count = (job.retry_count or 0) + 1
        job.error_message = error_message
        job.processing_log = (job.processing_log or "") + (f"FAILED: {error_message}\\n")
        job.completed_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(job)
        return job

    def append_log(self, job: UploadJob, message: str) -> UploadJob:
        """processing_log에 메시지를 추가하고 저장한다."""
        job.processing_log = (job.processing_log or "") + (f"{message}\\n")
        self.db.commit()
        self.db.refresh(job)
        return job

    def count_by_status(self, status: str) -> int:
        """상태별 UploadJob 건수를 반환한다."""
        return (
            self.db.query(func.count(UploadJob.id))
            .filter(UploadJob.status == status)
            .scalar()
            or 0
        )

    def count_completed_today(self) -> int:
        """오늘 완료된 UploadJob 건수를 반환한다."""
        start = datetime.now(timezone.utc).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return (
            self.db.query(func.count(UploadJob.id))
            .filter(UploadJob.status == UploadJobStatus.COMPLETED)
            .filter(UploadJob.completed_at >= start)
            .scalar()
            or 0
        )
