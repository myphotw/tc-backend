"""공통 Health / Dashboard API."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.common.services.monitoring_service import build_dashboard, check_health

router = APIRouter(
    prefix="/api/common",
    tags=["Monitoring"],
)


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    """운영 컴포넌트 Health Check."""
    return check_health(db)


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)) -> dict[str, object]:
    """Upload / Vision / API Usage / Storage / Worker 대시보드."""
    return build_dashboard(db)
