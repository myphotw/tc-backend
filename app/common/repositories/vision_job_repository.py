"""Vision Queue 저장소."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import logging
from typing import Iterator

from sqlalchemy import func, or_, update
from sqlalchemy.orm import Session

from app.common.models.vision_job import CommonVisionJob
from app.common.models.worker_status import CommonWorkerStatus


logger = logging.getLogger(__name__)


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

    @contextmanager
    def _commit_keep_state(self) -> Iterator[None]:
        previous = self.db.expire_on_commit
        self.db.expire_on_commit = False
        try:
            yield
            self.db.commit()
        finally:
            self.db.expire_on_commit = previous

    def create(
        self,
        *,
        file_id: int,
        priority: int,
        vision_provider: str = VisionProvider.GOOGLE,
        skip_duplicate_check: bool = False,
    ) -> CommonVisionJob | None:
        """
        Vision Queue를 생성한다.

        동일 file_id Queue가 이미 있거나 Vision 완료된 경우 생성하지 않는다.
        """
        if not skip_duplicate_check:
            status = self.get_blocking_status(file_id=file_id)
            if status is not None:
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
        self.db.flush()
        with self._commit_keep_state():
            pass
        return job

    def get_blocking_status(self, *, file_id: int) -> str | None:
        """
        Queue 생성을 막을 상태(WAITING/PROCESSING/COMPLETED)가 있으면 반환한다.

        한 번의 조회로 exists + is_vision_completed를 대체한다.
        """
        row = (
            self.db.query(CommonVisionJob.status)
            .filter(CommonVisionJob.file_id == file_id)
            .filter(CommonVisionJob.deleted.is_(False))
            .filter(
                CommonVisionJob.status.in_(
                    [
                        VisionJobStatus.WAITING,
                        VisionJobStatus.PROCESSING,
                        VisionJobStatus.COMPLETED,
                    ]
                )
            )
            .order_by(CommonVisionJob.id.desc())
            .first()
        )
        if row is None:
            return None
        return str(row[0])

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

    def mark_processing(self, job: CommonVisionJob) -> CommonVisionJob | None:
        """WAITING/active row만 원자적으로 claim한다."""
        result = self.db.execute(
            update(CommonVisionJob)
            .where(CommonVisionJob.id == job.id)
            .where(CommonVisionJob.status == VisionJobStatus.WAITING)
            .where(CommonVisionJob.deleted.is_(False))
            .values(
                status=VisionJobStatus.PROCESSING,
                started_at=datetime.now(timezone.utc),
                completed_at=None,
                last_error=None,
            )
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        with self._commit_keep_state():
            pass
        self.db.refresh(job)
        return job

    def mark_completed(self, job: CommonVisionJob) -> CommonVisionJob:
        """Vision Queue를 COMPLETED로 변경한다."""
        job.status = VisionJobStatus.COMPLETED
        job.completed_at = datetime.now(timezone.utc)
        job.last_error = None
        with self._commit_keep_state():
            pass
        return job

    def mark_waiting(self, job: CommonVisionJob) -> CommonVisionJob:
        """Return a quota-deferred job to WAITING without recording failure."""
        job.status = VisionJobStatus.WAITING
        job.started_at = None
        job.completed_at = None
        job.last_error = None
        with self._commit_keep_state():
            pass
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
        with self._commit_keep_state():
            pass
        return job

    def recover_stale_processing_jobs(
        self,
        *,
        stale_seconds: int,
        worker_name_prefix: str,
        live_heartbeat_seconds: int,
        limit: int = 50,
    ) -> int:
        """Return abandoned PROCESSING rows to WAITING without a retry penalty.

        A recently heartbeating Vision worker's current job is protected.  On
        PostgreSQL, row locks with SKIP LOCKED allow concurrent recovery loops
        to divide work without recovering the same row twice.
        """
        if stale_seconds <= 0 or live_heartbeat_seconds <= 0 or limit <= 0:
            return 0

        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(seconds=stale_seconds)
        live_after = now - timedelta(seconds=live_heartbeat_seconds)

        try:
            protected_ids = self._live_processing_job_ids(
                worker_name_prefix=worker_name_prefix,
                live_after=live_after,
            )
            recovered = self._recover_stale_rows(
                stale_before=stale_before,
                protected_ids=protected_ids,
                limit=limit,
            )
        except Exception:
            self.db.rollback()
            logger.exception("Failed to recover stale Vision jobs")
            return 0

        if recovered:
            logger.warning(
                "Recovered stale Vision jobs count=%s stale_seconds=%s",
                recovered,
                stale_seconds,
            )
        return recovered

    def _live_processing_job_ids(
        self,
        *,
        worker_name_prefix: str,
        live_after: datetime,
    ) -> set[int]:
        rows = (
            self.db.query(CommonWorkerStatus.current_job_id)
            .filter(CommonWorkerStatus.status == "RUNNING")
            .filter(CommonWorkerStatus.last_heartbeat.isnot(None))
            .filter(CommonWorkerStatus.last_heartbeat >= live_after)
            .filter(CommonWorkerStatus.current_job_id.isnot(None))
            .filter(
                or_(
                    CommonWorkerStatus.worker_name == worker_name_prefix,
                    CommonWorkerStatus.worker_name.like(
                        f"{worker_name_prefix}-%"
                    ),
                )
            )
            .all()
        )
        protected: set[int] = set()
        for row in rows:
            try:
                protected.add(int(row[0]))
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring non-numeric Vision current_job_id worker=%s value=%r",
                    worker_name_prefix,
                    row[0],
                )
        return protected

    def _stale_query(
        self,
        *,
        stale_before: datetime,
        protected_ids: set[int],
    ):
        query = (
            self.db.query(CommonVisionJob)
            .filter(CommonVisionJob.deleted.is_(False))
            .filter(CommonVisionJob.status == VisionJobStatus.PROCESSING)
            .filter(CommonVisionJob.completed_at.is_(None))
            .filter(CommonVisionJob.started_at.isnot(None))
            .filter(CommonVisionJob.started_at < stale_before)
        )
        if protected_ids:
            query = query.filter(CommonVisionJob.id.notin_(protected_ids))
        return query

    def _recover_stale_rows(
        self,
        *,
        stale_before: datetime,
        protected_ids: set[int],
        limit: int,
    ) -> int:
        query = self._stale_query(
            stale_before=stale_before,
            protected_ids=protected_ids,
        ).order_by(CommonVisionJob.started_at.asc(), CommonVisionJob.id.asc())
        if self.db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        jobs = query.limit(limit).all()
        for job in jobs:
            self._reset_stale_job(job)
        if jobs:
            with self._commit_keep_state():
                pass
        else:
            self.db.rollback()
        return len(jobs)

    @staticmethod
    def _reset_stale_job(job: CommonVisionJob) -> None:
        job.status = VisionJobStatus.WAITING
        job.started_at = None
        job.completed_at = None
        job.last_error = None

    def exists(self, *, file_id: int) -> bool:
        """
        동일 file_id의 활성 Vision Queue 존재 여부를 반환한다.

        WAITING / PROCESSING 상태만 중복으로 본다.
        """
        return (
            self.db.query(CommonVisionJob.id)
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
            self.db.query(CommonVisionJob.id)
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
