from __future__ import annotations

from pydantic import BaseModel

from app.common.schemas.external_api import PlateSolveResult


class PlateSolveReadProjection(BaseModel):
    plate_solve_status: str | None = None
    plate_solve_job_id: str | None = None
    plate_solve_result: PlateSolveResult | None = None
