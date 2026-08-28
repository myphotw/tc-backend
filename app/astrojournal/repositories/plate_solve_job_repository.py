from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.astrojournal.models.plate_solve_job import AstroPlateSolveJob


class PlateSolveJobStatus:
    WAITING = "WAITING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    ALL = (WAITING, PROCESSING, COMPLETED, FAILED)


class PlateSolveJobRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def enqueue(
        self,
        *,
        common_file_id: int,
        observation_record_id: str | None,
    ) -> tuple[AstroPlateSolveJob, bool]:
        existing = self.get_by_common_file_id(common_file_id)
        if existing is not None:
            if existing.observation_record_id is None and observation_record_id is not None:
                existing.observation_record_id = observation_record_id
                self.db.flush()
            return existing, False

        job = AstroPlateSolveJob(
            common_file_id=common_file_id,
            observation_record_id=observation_record_id,
            status=PlateSolveJobStatus.WAITING,
            attempts=0,
        )
        try:
            with self.db.begin_nested():
                self.db.add(job)
                self.db.flush()
            return job, True
        except IntegrityError:
            existing = self.get_by_common_file_id(common_file_id)
            if existing is None:
                raise
            return existing, False

    def get(self, job_id: str) -> AstroPlateSolveJob | None:
        return self.db.get(AstroPlateSolveJob, job_id)

    def get_by_common_file_id(self, common_file_id: int) -> AstroPlateSolveJob | None:
        return (
            self.db.query(AstroPlateSolveJob)
            .filter(AstroPlateSolveJob.common_file_id == common_file_id)
            .first()
        )

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> AstroPlateSolveJob | None:
        now = datetime.now(timezone.utc)
        job = (
            self.db.query(AstroPlateSolveJob)
            .filter(
                or_(
                    AstroPlateSolveJob.status == PlateSolveJobStatus.WAITING,
                    and_(
                        AstroPlateSolveJob.status == PlateSolveJobStatus.PROCESSING,
                        or_(
                            AstroPlateSolveJob.lease_expires_at.is_(None),
                            AstroPlateSolveJob.lease_expires_at < now,
                        ),
                    ),
                )
            )
            .order_by(AstroPlateSolveJob.created_at.asc(), AstroPlateSolveJob.id.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if job is None:
            return None
        job.status = PlateSolveJobStatus.PROCESSING
        if job.provider_submission_id is None and job.provider_job_id is None:
            job.attempts = int(job.attempts or 0) + 1
        job.started_at = now
        job.completed_at = None
        job.last_error = None
        job.worker_id = worker_id
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        self.db.flush()
        return job

    def touch_lease(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        now = datetime.now(timezone.utc)
        updated = (
            self.db.query(AstroPlateSolveJob)
            .filter(AstroPlateSolveJob.id == job_id)
            .filter(AstroPlateSolveJob.status == PlateSolveJobStatus.PROCESSING)
            .filter(AstroPlateSolveJob.worker_id == worker_id)
            .update(
                {
                    AstroPlateSolveJob.lease_expires_at: now
                    + timedelta(seconds=lease_seconds),
                    AstroPlateSolveJob.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        return updated == 1

    def record_submission(
        self,
        *,
        job_id: str,
        worker_id: str,
        submission_id: int,
    ) -> AstroPlateSolveJob | None:
        job = self._owned_processing_job(job_id=job_id, worker_id=worker_id)
        if job is None:
            return None
        job.provider_submission_id = submission_id
        self.db.flush()
        return job

    def record_provider_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        provider_job_id: int,
    ) -> AstroPlateSolveJob | None:
        job = self._owned_processing_job(job_id=job_id, worker_id=worker_id)
        if job is None:
            return None
        job.provider_job_id = provider_job_id
        self.db.flush()
        return job

    def record_replacement_submission(
        self,
        *,
        job_id: str,
        worker_id: str,
        submission_id: int,
    ) -> AstroPlateSolveJob | None:
        """Replace confirmed-missing provider work after a new submit succeeds."""
        job = self._owned_processing_job(job_id=job_id, worker_id=worker_id)
        if job is None:
            return None
        job.provider_submission_id = submission_id
        job.provider_job_id = None
        job.attempts = int(job.attempts or 0) + 1
        self.db.flush()
        return job

    def mark_retryable(
        self,
        *,
        job_id: str,
        worker_id: str,
        error_message: str,
    ) -> AstroPlateSolveJob | None:
        """Release a transient provider failure without losing provider IDs."""
        job = self._owned_processing_job(job_id=job_id, worker_id=worker_id)
        if job is None:
            return None
        job.status = PlateSolveJobStatus.WAITING
        job.started_at = None
        job.completed_at = None
        job.last_error = error_message
        job.worker_id = None
        job.lease_expires_at = None
        self.db.flush()
        return job

    def mark_completed(
        self,
        *,
        job_id: str,
        worker_id: str,
        provider: dict[str, object],
    ) -> AstroPlateSolveJob | None:
        job = self._owned_processing_job(job_id=job_id, worker_id=worker_id)
        if job is None:
            return None
        job.status = PlateSolveJobStatus.COMPLETED
        job.provider_job_id = _optional_int(provider.get("provider_job_id"))
        for field in (
            "ra",
            "dec",
            "rotation",
            "pixel_scale",
            "field_width",
            "field_height",
            "parity",
        ):
            value = provider.get(field)
            setattr(job, field, float(value) if value is not None else None)
        job.completed_at = datetime.now(timezone.utc)
        job.last_error = None
        job.worker_id = None
        job.lease_expires_at = None
        self.db.flush()
        return job

    def mark_failed(
        self,
        *,
        job_id: str,
        worker_id: str,
        error_message: str,
    ) -> AstroPlateSolveJob | None:
        job = self._owned_processing_job(job_id=job_id, worker_id=worker_id)
        if job is None:
            return None
        job.status = PlateSolveJobStatus.FAILED
        job.completed_at = datetime.now(timezone.utc)
        job.last_error = error_message
        job.worker_id = None
        job.lease_expires_at = None
        self.db.flush()
        return job

    def retry(self, job: AstroPlateSolveJob) -> AstroPlateSolveJob:
        """Requeue a failed job while retaining all reusable provider identifiers."""
        job.status = PlateSolveJobStatus.WAITING
        for field in (
            "ra",
            "dec",
            "rotation",
            "pixel_scale",
            "field_width",
            "field_height",
            "parity",
        ):
            setattr(job, field, None)
        job.started_at = None
        job.completed_at = None
        job.last_error = None
        job.worker_id = None
        job.lease_expires_at = None
        self.db.flush()
        return job

    def fail_waiting_for_file(
        self,
        *,
        common_file_id: int,
        error_message: str,
    ) -> AstroPlateSolveJob | None:
        job = self.get_by_common_file_id(common_file_id)
        if job is None or job.status != PlateSolveJobStatus.WAITING:
            return None
        job.status = PlateSolveJobStatus.FAILED
        job.completed_at = datetime.now(timezone.utc)
        job.last_error = error_message
        self.db.flush()
        return job

    def count_by_status(self) -> dict[str, int]:
        counts = {status: 0 for status in PlateSolveJobStatus.ALL}
        rows = (
            self.db.query(AstroPlateSolveJob.status, func.count(AstroPlateSolveJob.id))
            .group_by(AstroPlateSolveJob.status)
            .all()
        )
        for status, count in rows:
            if status in counts:
                counts[str(status)] = int(count)
        return counts

    def _owned_processing_job(
        self,
        *,
        job_id: str,
        worker_id: str,
    ) -> AstroPlateSolveJob | None:
        return (
            self.db.query(AstroPlateSolveJob)
            .filter(AstroPlateSolveJob.id == job_id)
            .filter(AstroPlateSolveJob.status == PlateSolveJobStatus.PROCESSING)
            .filter(AstroPlateSolveJob.worker_id == worker_id)
            .with_for_update()
            .first()
        )


def _optional_int(value: object) -> int | None:
    return int(value) if value is not None else None
