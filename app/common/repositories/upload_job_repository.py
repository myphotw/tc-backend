"""Upload job 저장소."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.common.models.upload_job import UploadJob

logger = logging.getLogger(__name__)

_CLAIMED_WORKER_RE = re.compile(r"CLAIMED worker=([^\s\\]+)")


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

    @contextmanager
    def _commit_keep_state(self) -> Iterator[None]:
        """commit 후 refresh SELECT를 피하기 위해 expire_on_commit을 잠시 끈다."""
        previous = self.db.expire_on_commit
        self.db.expire_on_commit = False
        try:
            yield
            self.db.commit()
        finally:
            self.db.expire_on_commit = previous

    def _dialect_name(self) -> str:
        bind = self.db.get_bind()
        return bind.dialect.name if bind is not None else ""

    def create_waiting_job(
        self,
        *,
        job_id: str,
        source_type: str,
        incoming_path: str,
        service_name: str = "MemoryKeeper",
        client_file_id: str | None = None,
        client_content_sha256: str | None = None,
        processing_log: str | None = None,
    ) -> UploadJob:
        """WAITING 상태의 업로드 작업을 생성한다."""
        job = UploadJob(
            job_id=job_id,
            source_type=source_type,
            status=UploadJobStatus.WAITING,
            incoming_path=incoming_path,
            service_name=service_name,
            client_file_id=client_file_id,
            client_content_sha256=client_content_sha256,
            processing_log=processing_log,
        )
        self.db.add(job)
        self.db.flush()
        with self._commit_keep_state():
            pass
        return job

    def get_by_client_file_id(
        self,
        *,
        service_name: str,
        client_file_id: str,
    ) -> UploadJob | None:
        """Return an existing service-scoped idempotency job."""
        return (
            self.db.query(UploadJob)
            .filter(UploadJob.service_name == service_name)
            .filter(UploadJob.client_file_id == client_file_id)
            .first()
        )

    def set_client_content_sha256_if_missing(
        self,
        job: UploadJob,
        *,
        client_content_sha256: str,
    ) -> UploadJob:
        """Backfill a hash supplied by a later retry without changing job state."""
        if job.client_content_sha256:
            return job
        job.client_content_sha256 = client_content_sha256
        with self._commit_keep_state():
            pass
        return job

    def get_next_waiting_job(self) -> UploadJob | None:
        """
        가장 오래된 WAITING 작업을 조회한다.

        Worker 실행 경로에서는 claim_next_waiting_job을 사용한다.
        """
        return (
            self.db.query(UploadJob)
            .filter(UploadJob.status == UploadJobStatus.WAITING)
            .order_by(UploadJob.created_at.asc(), UploadJob.id.asc())
            .first()
        )

    def claim_next_waiting_job(self, worker_id: str) -> UploadJob | None:
        """
        WAITING Job 1건을 원자적으로 claim하여 PROCESSING으로 바꾼다.

        PostgreSQL: SELECT ... FOR UPDATE SKIP LOCKED 후 동일 트랜잭션에서 UPDATE.
        SQLite 등: 동등한 atomic UPDATE fallback.
        claim transaction은 짧게 끝내고 lock을 즉시 해제한다.
        """
        now = datetime.now(timezone.utc)
        claim_line = f"CLAIMED worker={worker_id}\\n"
        dialect = self._dialect_name()

        try:
            if dialect == "postgresql":
                job = self._claim_postgres(worker_id=worker_id, now=now, claim_line=claim_line)
            else:
                job = self._claim_fallback(worker_id=worker_id, now=now, claim_line=claim_line)
        except Exception:
            self.db.rollback()
            logger.exception("claim_next_waiting_job failed worker_id=%s", worker_id)
            return None

        return job

    def _claim_postgres(
        self,
        *,
        worker_id: str,
        now: datetime,
        claim_line: str,
    ) -> UploadJob | None:
        """
        PostgreSQL SKIP LOCKED claim.

        실제 SQL 형태:
        SELECT ... FROM common_upload_jobs
        WHERE status='WAITING'
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        FOR UPDATE SKIP LOCKED;
        -- 이어 UPDATE status/started_at/processing_log 후 COMMIT
        """
        job = (
            self.db.query(UploadJob)
            .filter(UploadJob.status == UploadJobStatus.WAITING)
            .order_by(UploadJob.created_at.asc(), UploadJob.id.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if job is None:
            self.db.commit()
            return None

        job.status = UploadJobStatus.PROCESSING
        job.started_at = now
        job.completed_at = None
        job.error_message = None
        job.processing_log = (job.processing_log or "") + claim_line + "START\\n"
        with self._commit_keep_state():
            pass
        logger.info(
            "Claimed upload job_id=%s worker_id=%s dialect=postgresql",
            job.job_id,
            worker_id,
        )
        return job

    def _claim_fallback(
        self,
        *,
        worker_id: str,
        now: datetime,
        claim_line: str,
    ) -> UploadJob | None:
        """
        SKIP LOCKED 미지원 DB용 claim fallback.

        row를 읽어 status=WAITING일 때만 PROCESSING으로 바꾼 뒤 commit한다.
        동시 작성자가 있으면 Integrity/경합 시 재시도하지 않고 None/다른 row로 넘어갈 수 있다.
        """
        job = (
            self.db.query(UploadJob)
            .filter(UploadJob.status == UploadJobStatus.WAITING)
            .order_by(UploadJob.created_at.asc(), UploadJob.id.asc())
            .first()
        )
        if job is None:
            self.db.commit()
            return None

        # Optimistic conditional update (prevents double claim under races).
        new_log = (job.processing_log or "") + claim_line + "START\\n"
        result = self.db.execute(
            update(UploadJob)
            .where(UploadJob.id == job.id)
            .where(UploadJob.status == UploadJobStatus.WAITING)
            .values(
                status=UploadJobStatus.PROCESSING,
                started_at=now,
                completed_at=None,
                error_message=None,
                processing_log=new_log,
            )
        )
        if not result.rowcount:
            self.db.rollback()
            return None

        with self._commit_keep_state():
            pass
        claimed = self.db.query(UploadJob).filter(UploadJob.id == job.id).first()
        logger.info(
            "Claimed upload job_id=%s worker_id=%s dialect=%s",
            getattr(claimed, "job_id", None),
            worker_id,
            self._dialect_name(),
        )
        return claimed

    def recover_stale_processing_jobs(
        self,
        *,
        stale_seconds: int,
        worker_id: str,
        limit: int = 50,
    ) -> int:
        """
        stale PROCESSING Job을 WAITING으로 복구한다.

        컬럼 추가 없이 started_at을 lease 시각으로 사용한다.
        FOR UPDATE SKIP LOCKED(또는 fallback)로 Worker 간 중복 복구를 방지한다.
        """
        if stale_seconds <= 0:
            return 0

        threshold = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
        dialect = self._dialect_name()
        recovered = 0

        try:
            if dialect == "postgresql":
                jobs = (
                    self.db.query(UploadJob)
                    .filter(UploadJob.status == UploadJobStatus.PROCESSING)
                    .filter(UploadJob.completed_at.is_(None))
                    .filter(UploadJob.started_at.isnot(None))
                    .filter(UploadJob.started_at < threshold)
                    .order_by(UploadJob.started_at.asc(), UploadJob.id.asc())
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                    .all()
                )
                for job in jobs:
                    previous_worker = self.extract_claimed_worker(job.processing_log)
                    job.status = UploadJobStatus.WAITING
                    job.started_at = None
                    job.error_message = None
                    job.retry_count = (job.retry_count or 0) + 1
                    job.processing_log = (job.processing_log or "") + (
                        f"STALE_JOB_RECOVERED previous_worker="
                        f"{previous_worker or 'unknown'} by={worker_id}\\n"
                    )
                    recovered += 1
                if recovered:
                    with self._commit_keep_state():
                        pass
            else:
                recovered = self._recover_stale_fallback(
                    threshold=threshold,
                    worker_id=worker_id,
                    limit=limit,
                )
        except Exception:
            self.db.rollback()
            logger.exception("recover_stale_processing_jobs failed worker_id=%s", worker_id)
            return 0

        if recovered:
            logger.warning(
                "Recovered stale upload jobs count=%s worker_id=%s stale_seconds=%s",
                recovered,
                worker_id,
                stale_seconds,
            )
        return recovered

    def _recover_stale_fallback(
        self,
        *,
        threshold: datetime,
        worker_id: str,
        limit: int,
    ) -> int:
        """SQLite 등에서 row-by-row conditional UPDATE로 stale을 복구한다."""
        ids = [
            row[0]
            for row in (
                self.db.query(UploadJob.id)
                .filter(UploadJob.status == UploadJobStatus.PROCESSING)
                .filter(UploadJob.completed_at.is_(None))
                .filter(UploadJob.started_at.isnot(None))
                .filter(UploadJob.started_at < threshold)
                .order_by(UploadJob.started_at.asc(), UploadJob.id.asc())
                .limit(limit)
                .all()
            )
        ]
        recovered = 0
        for job_id in ids:
            job = (
                self.db.query(UploadJob)
                .filter(UploadJob.id == job_id)
                .filter(UploadJob.status == UploadJobStatus.PROCESSING)
                .filter(UploadJob.completed_at.is_(None))
                .filter(UploadJob.started_at < threshold)
                .first()
            )
            if job is None:
                continue
            previous_worker = self.extract_claimed_worker(job.processing_log)
            new_log = (job.processing_log or "") + (
                f"STALE_JOB_RECOVERED previous_worker="
                f"{previous_worker or 'unknown'} by={worker_id}\\n"
            )
            result = self.db.execute(
                update(UploadJob)
                .where(UploadJob.id == job.id)
                .where(UploadJob.status == UploadJobStatus.PROCESSING)
                .where(UploadJob.completed_at.is_(None))
                .values(
                    status=UploadJobStatus.WAITING,
                    started_at=None,
                    error_message=None,
                    retry_count=(job.retry_count or 0) + 1,
                    processing_log=new_log,
                )
            )
            if result.rowcount:
                recovered += 1
        if recovered:
            with self._commit_keep_state():
                pass
        else:
            self.db.rollback()
        return recovered

    def touch_processing_lease(self, *, job_id: str) -> bool:
        """
        PROCESSING Job의 lease(started_at)를 갱신한다.

        updated_at 컬럼이 없어 started_at을 heartbeat lease로 사용한다.
        """
        now = datetime.now(timezone.utc)
        result = self.db.execute(
            update(UploadJob)
            .where(UploadJob.job_id == job_id)
            .where(UploadJob.status == UploadJobStatus.PROCESSING)
            .where(UploadJob.completed_at.is_(None))
            .values(started_at=now)
        )
        if not result.rowcount:
            self.db.rollback()
            return False
        with self._commit_keep_state():
            pass
        return True

    @staticmethod
    def extract_claimed_worker(processing_log: str | None) -> str | None:
        """processing_log에서 마지막 CLAIMED worker 값을 추출한다."""
        if not processing_log:
            return None
        matches = _CLAIMED_WORKER_RE.findall(processing_log.replace("\\n", "\n"))
        if not matches:
            return None
        return matches[-1]

    def mark_processing(self, job: UploadJob) -> UploadJob:
        """작업을 PROCESSING 상태로 변경한다. (claim 미사용 경로 호환용)"""
        job.status = UploadJobStatus.PROCESSING
        job.started_at = datetime.now(timezone.utc)
        job.processing_log = (job.processing_log or "") + ("START\\n")
        job.error_message = None
        with self._commit_keep_state():
            pass
        return job

    def mark_completed(self, job: UploadJob, *, file_id: str) -> UploadJob | None:
        """
        작업을 COMPLETED로 변경한다.

        PROCESSING인 경우에만 성공하여 중복 완료를 방지한다.
        """
        now = datetime.now(timezone.utc)
        new_log = (job.processing_log or "") + "COMPLETED\\n"
        result = self.db.execute(
            update(UploadJob)
            .where(UploadJob.job_id == job.job_id)
            .where(UploadJob.status == UploadJobStatus.PROCESSING)
            .values(
                status=UploadJobStatus.COMPLETED,
                file_id=file_id,
                completed_at=now,
                error_message=None,
                processing_log=new_log,
            )
        )
        if not result.rowcount:
            self.db.rollback()
            logger.warning(
                "mark_completed skipped (not PROCESSING) job_id=%s",
                job.job_id,
            )
            return None
        with self._commit_keep_state():
            pass
        job.status = UploadJobStatus.COMPLETED
        job.file_id = file_id
        job.completed_at = now
        job.processing_log = new_log
        return job

    def mark_failed(self, job: UploadJob, *, error_message: str) -> UploadJob | None:
        """작업을 FAILED로 변경한다. PROCESSING인 경우만."""
        now = datetime.now(timezone.utc)
        current = self.get(job.job_id) or job
        next_retry = (current.retry_count or 0) + 1
        base_log = job.processing_log if job.processing_log else current.processing_log
        new_log = (base_log or "") + f"FAILED: {error_message}\\n"
        result = self.db.execute(
            update(UploadJob)
            .where(UploadJob.job_id == job.job_id)
            .where(UploadJob.status == UploadJobStatus.PROCESSING)
            .values(
                status=UploadJobStatus.FAILED,
                retry_count=next_retry,
                error_message=error_message,
                completed_at=now,
                processing_log=new_log,
            )
        )
        if not result.rowcount:
            self.db.rollback()
            return None
        with self._commit_keep_state():
            pass
        job.status = UploadJobStatus.FAILED
        job.retry_count = next_retry
        job.error_message = error_message
        job.completed_at = now
        job.processing_log = new_log
        return job

    def append_log(self, job: UploadJob, message: str) -> UploadJob:
        """
        processing_log에 메시지를 추가한다.

        매 로그마다 commit/refresh 하지 않는다.
        """
        job.processing_log = (job.processing_log or "") + (f"{message}\\n")
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

    def get(self, job_id: str) -> UploadJob | None:
        """job_id로 UploadJob을 조회한다."""
        return (
            self.db.query(UploadJob)
            .filter(UploadJob.job_id == job_id)
            .first()
        )

    def list(
        self,
        *,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: str = "created_at_desc",
    ) -> tuple[list[UploadJob], int]:
        """UploadJob 목록을 페이징 조회한다."""
        query = self.db.query(UploadJob)
        if status:
            query = query.filter(UploadJob.status == status)

        total = query.count()
        query = self._apply_sort(query, sort)
        items = (
            query.offset(max(page - 1, 0) * page_size)
            .limit(page_size)
            .all()
        )
        return items, total

    def _apply_sort(self, query, sort: str):
        normalized = (sort or "created_at_desc").lower()
        if normalized in {"created_at_asc", "requested_at_asc"}:
            return query.order_by(UploadJob.created_at.asc(), UploadJob.id.asc())
        if normalized in {"started_at_desc"}:
            return query.order_by(
                UploadJob.started_at.desc().nullslast(),
                UploadJob.id.desc(),
            )
        if normalized in {"started_at_asc"}:
            return query.order_by(
                UploadJob.started_at.asc().nullslast(),
                UploadJob.id.asc(),
            )
        if normalized in {"completed_at_desc"}:
            return query.order_by(
                UploadJob.completed_at.desc().nullslast(),
                UploadJob.id.desc(),
            )
        if normalized in {"completed_at_asc"}:
            return query.order_by(
                UploadJob.completed_at.asc().nullslast(),
                UploadJob.id.asc(),
            )
        if normalized in {"status_asc"}:
            return query.order_by(UploadJob.status.asc(), UploadJob.id.asc())
        if normalized in {"status_desc"}:
            return query.order_by(UploadJob.status.desc(), UploadJob.id.desc())
        return query.order_by(UploadJob.created_at.desc(), UploadJob.id.desc())
