from __future__ import annotations

from sqlalchemy.orm import Session

from app.astrojournal.models.plate_solve_job import AstroPlateSolveJob
from app.astrojournal.repositories.plate_solve_job_repository import (
    PlateSolveJobRepository,
    PlateSolveJobStatus,
)
from app.astrojournal.schemas.plate_solve import PlateSolveReadProjection
from app.common.schemas.external_api import PlateSolveResult


class PlateSolveReadService:
    """Build the shared Observation/Gallery Plate Solve read projection."""

    def __init__(self, db: Session) -> None:
        self.repository = PlateSolveJobRepository(db)

    def for_file(
        self,
        *,
        common_file_id: int,
        fallback_status: str | None,
        include_result: bool,
    ) -> PlateSolveReadProjection:
        return self.from_job(
            self.repository.get_by_common_file_id(common_file_id),
            fallback_status=fallback_status,
            include_result=include_result,
        )

    @staticmethod
    def from_job(
        job: AstroPlateSolveJob | None,
        *,
        fallback_status: str | None,
        include_result: bool,
    ) -> PlateSolveReadProjection:
        if job is None:
            return PlateSolveReadProjection(plate_solve_status=fallback_status)

        result = None
        if include_result and job.status == PlateSolveJobStatus.COMPLETED:
            result = PlateSolveResult(
                ra=job.ra,
                dec=job.dec,
                rotation=job.rotation,
                pixel_scale=job.pixel_scale,
                field_width=job.field_width,
                field_height=job.field_height,
                parity=job.parity,
            )
        return PlateSolveReadProjection(
            plate_solve_status=job.status,
            plate_solve_job_id=str(job.id),
            plate_solve_result=result,
        )
