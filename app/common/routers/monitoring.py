"""공통 Health / Dashboard API."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.common.services.monitoring_service import (
    build_dashboard,
    check_external_readiness,
    check_health,
)

router = APIRouter(
    prefix="/api/common",
    tags=["Monitoring"],
)


@router.get(
    "/health",
    summary="Health Check",
    description="Database / Storage / Vision / Weather / Geocoding 상태를 점검한다.",
    response_description="컴포넌트별 OK/FAIL 및 버전 정보",
)
def health(db: Session = Depends(get_db)) -> dict[str, object]:
    """운영 컴포넌트 Health Check."""
    return check_health(db)


@router.get(
    "/readiness",
    summary="External service runtime readiness",
)
def readiness(db: Session = Depends(get_db)) -> dict[str, object]:
    return check_external_readiness(db)


@router.get(
    "/dashboard",
    summary="Operations Dashboard",
    description=(
        "Upload/Vision Queue 상태, API Usage, Storage 파일 수, "
        "Worker heartbeat 상태를 반환한다."
    ),
    response_description="운영 대시보드 집계 결과",
)
def dashboard(db: Session = Depends(get_db)) -> dict[str, object]:
    """Upload / Vision / API Usage / Storage / Worker 대시보드."""
    return build_dashboard(db)
