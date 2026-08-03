from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.common.models.vision_job import CommonVisionJob


class VisionJobStatus:
    """Vision Queue 상태 값."""

    WAITING = "WAITING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class VisionProvider:
    """Vision Provider 값."""

    GOOGLE = "GOOGLE"
    AZURE = "AZURE"
    AWS = "AWS"
    LOCAL = "LOCAL"


class VisionJobRepository:
    """common_vision_jobs 저장소."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        file_id: int,
        priority: int,
        vision_provider: str = VisionProvider.GOOGLE,
    ) -> CommonVisionJob | None:
        """
        Vision Queue를 생성한다.

        동일 file_id Queue가 이미 있거나 Vision 완료된 경우 생성하지 않는다.
        """
        if self.exists(file_id=file_id):
            return None
        if self.is_vision_completed(file_id=file_id):
            return None

        job = CommonVisionJob(
            file_id=file_id,
            priority=priority,
            status=VisionJobStatus.WAITING,
            retry_count=0,
            vision_provider=vision_provider,
            requested_at=datetime.now(timezone.utc),
            deleted=False,
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def get_waiting_jobs(self, *, limit: int = 50) -> list[CommonVisionJob]:
        """WAITING 상태의 Vision Queue를 priority 내림차순으로 조회한다."""
        return (
            self.db.query(CommonVisionJob)
            .filter(CommonVisionJob.status == VisionJobStatus.WAITING)
            .filter(CommonVisionJob.deleted.is_(False))
            .order_by(
                CommonVisionJob.priority.desc(),
                CommonVisionJob.requested_at.desc(),
                CommonVisionJob.id.desc(),
            )
            .limit(limit)
            .all()
        )

    def get_next_waiting_job(self) -> CommonVisionJob | None:
        """WAITING 상태의 Vision Queue 1건을 가져온다."""
        jobs = self.get_waiting_jobs(limit=1)
        if not jobs:
            return None
        return jobs[0]

    def mark_processing(self, job: CommonVisionJob) -> CommonVisionJob:
        """Vision Queue를 PROCESSING으로 변경한다."""
        job.status = VisionJobStatus.PROCESSING
        job.started_at = datetime.now(timezone.utc)
        job.last_error = None
        self.db.commit()
        self.db.refresh(job)
        return job

    def mark_completed(self, job: CommonVisionJob) -> CommonVisionJob:
        """Vision Queue를 COMPLETED로 변경한다."""
        job.status = VisionJobStatus.COMPLETED
        job.completed_at = datetime.now(timezone.utc)
        job.last_error = None
        self.db.commit()
        self.db.refresh(job)
        return job

    def mark_failed(
        self,
        job: CommonVisionJob,
        *,
        error_message: str,
    ) -> CommonVisionJob:
        """Vision Queue를 FAILED로 변경한다."""
        job.status = VisionJobStatus.FAILED
        job.retry_count = (job.retry_count or 0) + 1
        job.last_error = error_message
        job.completed_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(job)
        return job

    def exists(self, *, file_id: int) -> bool:
        """
        동일 file_id의 활성 Vision Queue 존재 여부를 반환한다.

        WAITING / PROCESSING 상태만 중복으로 본다.
        """
        return (
            self.db.query(CommonVisionJob)
            .filter(CommonVisionJob.file_id == file_id)
            .filter(CommonVisionJob.deleted.is_(False))
            .filter(
                CommonVisionJob.status.in_(
                    [
                        VisionJobStatus.WAITING,
                        VisionJobStatus.PROCESSING,
                    ]
                )
            )
            .first()
            is not None
        )

    def is_vision_completed(self, *, file_id: int) -> bool:
        """동일 file_id의 Vision COMPLETED 이력이 있는지 확인한다."""
        return (
            self.db.query(CommonVisionJob)
            .filter(CommonVisionJob.file_id == file_id)
            .filter(CommonVisionJob.deleted.is_(False))
            .filter(CommonVisionJob.status == VisionJobStatus.COMPLETED)
            .first()
            is not None
        )

    def count_by_status(self, status: str) -> int:
        """상태별 VisionJob 건수를 반환한다."""
        return (
            self.db.query(func.count(CommonVisionJob.id))
            .filter(CommonVisionJob.deleted.is_(False))
            .filter(CommonVisionJob.status == status)
            .scalar()
            or 0
        )

    def count_completed_today(self) -> int:
        """오늘 완료된 VisionJob 건수를 반환한다."""
        start = datetime.now(timezone.utc).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        return (
            self.db.query(func.count(CommonVisionJob.id))
            .filter(CommonVisionJob.deleted.is_(False))
            .filter(CommonVisionJob.status == VisionJobStatus.COMPLETED)
            .filter(CommonVisionJob.completed_at >= start)
            .scalar()
            or 0
        )
