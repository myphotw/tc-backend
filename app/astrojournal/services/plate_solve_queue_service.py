from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.orm import Session

from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.models.plate_solve_job import AstroPlateSolveJob
from app.astrojournal.repositories.plate_solve_job_repository import (
    PlateSolveJobRepository,
    PlateSolveJobStatus,
)
from app.common.repositories.change_event_repository import (
    ChangeEventRepository,
    ChangeOperation,
)
from app.common.services.api_clients.base_client import (
    ApiClientError,
    ExternalApiErrorCode,
)


class PlateSolveQueueService:
    SERVICE_NAME = "AstroJournal"
    RESOURCE_TYPE = "ObservationRecord"

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = PlateSolveJobRepository(db)

    def enqueue(
        self,
        *,
        common_file_id: int,
        observation_record_id: str | None,
        commit: bool = True,
    ) -> tuple[AstroPlateSolveJob, bool]:
        job, created = self.repository.enqueue(
            common_file_id=common_file_id,
            observation_record_id=observation_record_id,
        )
        if commit:
            with self._commit_keep_state():
                pass
        return job, created

    def get_optional(self, job_id: str) -> AstroPlateSolveJob | None:
        return self.repository.get(job_id)

    def get_for_file(self, common_file_id: int) -> AstroPlateSolveJob | None:
        return self.repository.get_by_common_file_id(common_file_id)

    def response(self, job: AstroPlateSolveJob) -> dict[str, object]:
        result = None
        if job.status == PlateSolveJobStatus.COMPLETED:
            result = {
                "ra": job.ra,
                "dec": job.dec,
                "rotation": job.rotation,
                "pixel_scale": job.pixel_scale,
                "field_width": job.field_width,
                "field_height": job.field_height,
                "parity": job.parity,
            }
        return {
            "job_id": job.id,
            "status": job.status,
            "common_file_id": job.common_file_id,
            "provider": job.provider,
            "result": result,
            "provider_metadata": {
                "submission_id": job.provider_submission_id,
                "provider_job_id": job.provider_job_id,
            },
        }

    def summary(self) -> dict[str, int]:
        counts = self.repository.count_by_status()
        return {"total": sum(counts.values()), **counts}

    def retry(self, job_id: str) -> AstroPlateSolveJob:
        job = self.repository.get(job_id)
        if job is None:
            self._raise_invalid_job()
        if job.status != PlateSolveJobStatus.FAILED:
            raise ApiClientError(
                "Only FAILED Plate Solve jobs can be retried",
                code=ExternalApiErrorCode.INVALID_REQUEST,
            )
        self.repository.retry(job)
        self._sync_observation_status(job, PlateSolveJobStatus.WAITING)
        with self._commit_keep_state():
            pass
        return job

    def fail_waiting_if_file_unreferenced(
        self,
        *,
        common_file_id: int,
        commit: bool = True,
    ) -> AstroPlateSolveJob | None:
        active_record = (
            self.db.query(ObservationRecord.id)
            .filter(ObservationRecord.file_id == common_file_id)
            .filter(ObservationRecord.service_name == self.SERVICE_NAME)
            .filter(ObservationRecord.deleted_at.is_(None))
            .first()
        )
        if active_record is not None:
            return None
        job = self.repository.fail_waiting_for_file(
            common_file_id=common_file_id,
            error_message="Observation record was deleted before Plate Solve started",
        )
        if job is not None and commit:
            with self._commit_keep_state():
                pass
        return job

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> AstroPlateSolveJob | None:
        job = self.repository.claim_next(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if job is None:
            self.db.rollback()
            return None
        # Release the queue row before touching ObservationRecord. Keeping both
        # locks in one transaction would invert the delete/reset lock order.
        with self._commit_keep_state():
            pass
        self._sync_observation_status(job, PlateSolveJobStatus.PROCESSING)
        with self._commit_keep_state():
            pass
        return job

    def touch_lease(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        touched = self.repository.touch_lease(
            job_id=job_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        self.db.commit()
        return touched

    def record_submission(
        self,
        *,
        job_id: str,
        worker_id: str,
        submission_id: int,
    ) -> None:
        job = self.repository.record_submission(
            job_id=job_id,
            worker_id=worker_id,
            submission_id=submission_id,
        )
        if job is None:
            self.db.rollback()
            raise RuntimeError("Plate Solve job lease was lost")
        self.db.commit()

    def record_provider_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        provider_job_id: int,
    ) -> None:
        job = self.repository.record_provider_job(
            job_id=job_id,
            worker_id=worker_id,
            provider_job_id=provider_job_id,
        )
        if job is None:
            self.db.rollback()
            raise RuntimeError("Plate Solve job lease was lost")
        self.db.commit()

    def requeue_transient(
        self,
        *,
        job_id: str,
        worker_id: str,
        error_message: str,
    ) -> AstroPlateSolveJob:
        job = self.repository.mark_retryable(
            job_id=job_id,
            worker_id=worker_id,
            error_message=error_message,
        )
        if job is None:
            self.db.rollback()
            raise RuntimeError("Plate Solve job lease was lost")
        self._sync_observation_status(job, PlateSolveJobStatus.WAITING)
        with self._commit_keep_state():
            pass
        return job

    def complete(
        self,
        *,
        job_id: str,
        worker_id: str,
        provider: dict[str, object],
    ) -> AstroPlateSolveJob:
        job = self.repository.mark_completed(
            job_id=job_id,
            worker_id=worker_id,
            provider=provider,
        )
        if job is None:
            self.db.rollback()
            raise RuntimeError("Plate Solve job lease was lost")
        self._sync_observation_status(job, PlateSolveJobStatus.COMPLETED)
        with self._commit_keep_state():
            pass
        return job

    def fail(
        self,
        *,
        job_id: str,
        worker_id: str,
        error_message: str,
    ) -> AstroPlateSolveJob:
        job = self.repository.mark_failed(
            job_id=job_id,
            worker_id=worker_id,
            error_message=error_message,
        )
        if job is None:
            self.db.rollback()
            raise RuntimeError("Plate Solve job lease was lost")
        self._sync_observation_status(job, PlateSolveJobStatus.FAILED)
        with self._commit_keep_state():
            pass
        return job

    def _sync_observation_status(
        self,
        job: AstroPlateSolveJob,
        status: str,
    ) -> None:
        records = (
            self.db.query(ObservationRecord)
            .filter(ObservationRecord.file_id == job.common_file_id)
            .filter(ObservationRecord.service_name == self.SERVICE_NAME)
            .filter(ObservationRecord.deleted_at.is_(None))
            .with_for_update()
            .all()
        )
        for record in records:
            if record.plate_solve_status == status:
                continue
            record.plate_solve_status = status
            record.revision = int(record.revision or 0) + 1
            self.db.flush()
            ChangeEventRepository(self.db).append(
                service_name=self.SERVICE_NAME,
                resource_type=self.RESOURCE_TYPE,
                resource_id=record.id,
                operation=ChangeOperation.UPDATE,
                revision=record.revision,
            )

    @contextmanager
    def _commit_keep_state(self) -> Iterator[None]:
        previous = self.db.expire_on_commit
        self.db.expire_on_commit = False
        try:
            yield
            self.db.commit()
        finally:
            self.db.expire_on_commit = previous

    @staticmethod
    def _raise_invalid_job() -> None:
        raise ApiClientError(
            "Plate solve job_id is invalid",
            code=ExternalApiErrorCode.INVALID_REQUEST,
        )
