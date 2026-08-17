from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.astrojournal.services.plate_solve_service import PlateSolveService
from app.common.database import get_db
from app.common.schemas.external_api import (
    PlateSolveCreateRequest,
    PlateSolveJobResponse,
)
from app.common.services.api_clients.base_client import (
    ApiClientError,
    ExternalApiErrorCode,
)

router = APIRouter(prefix="/api/astro/plate-solve", tags=["Astro Plate Solve"])


@router.post("", response_model=PlateSolveJobResponse, status_code=202)
def submit_plate_solve(
    data: PlateSolveCreateRequest,
    db: Session = Depends(get_db),
):
    return _call(PlateSolveService(db).submit, common_file_id=data.common_file_id)


@router.get("/{job_id}", response_model=PlateSolveJobResponse)
def get_plate_solve(job_id: str, db: Session = Depends(get_db)):
    return _call(PlateSolveService(db).get, job_id=job_id)


def _call(function, **kwargs):
    try:
        return function(**kwargs)
    except ApiClientError as exc:
        status_by_code = {
            ExternalApiErrorCode.API_KEY_NOT_CONFIGURED: 503,
            ExternalApiErrorCode.API_LIMIT_EXCEEDED: 429,
            ExternalApiErrorCode.PROVIDER_TIMEOUT: 504,
            ExternalApiErrorCode.PROVIDER_ERROR: 502,
            ExternalApiErrorCode.INVALID_REQUEST: 400,
        }
        raise HTTPException(
            status_code=status_by_code[exc.code],
            detail={"code": exc.code.value, "message": str(exc)},
        ) from exc
