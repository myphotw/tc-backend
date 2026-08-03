"""Health / Dashboard 모니터링 서비스."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.common.config import settings
from app.common.repositories.api_usage_repository import (
    ApiName,
    ApiProvider,
    ApiUsageRepository,
)
from app.common.repositories.upload_job_repository import (
    UploadJobRepository,
    UploadJobStatus,
)
from app.common.repositories.vision_job_repository import (
    VisionJobRepository,
    VisionJobStatus,
)
from app.common.repositories.worker_status_repository import WorkerStatusRepository


def check_health(db: Session) -> dict[str, str]:
    """운영 컴포넌트 Health Check 결과를 반환한다."""
    database = _check_database(db)
    storage = _check_storage()
    vision = _check_vision_credential()
    weather = _check_weather_key()
    geocoding = _check_geocoding_key()

    components = {
        "database": database,
        "storage": storage,
        "vision": vision,
        "weather": weather,
        "geocoding": geocoding,
    }
    status = "OK" if all(value == "OK" for value in components.values()) else "DEGRADED"
    return {
        "status": status,
        **components,
        "time": datetime.now(timezone.utc).isoformat(),
    }


def build_dashboard(db: Session) -> dict[str, object]:
    """운영 Dashboard 집계 결과를 반환한다."""
    upload_repo = UploadJobRepository(db)
    vision_repo = VisionJobRepository(db)
    usage_repo = ApiUsageRepository(db)
    worker_repo = WorkerStatusRepository(db)

    vision_usage = usage_repo.get_usage(
        provider=ApiProvider.GOOGLE,
        api_name=ApiName.VISION,
    )
    geocoding_usage = usage_repo.get_usage(
        provider=ApiProvider.GOOGLE,
        api_name=ApiName.GEOCODING,
    )
    weather_usage = usage_repo.get_usage(
        provider=ApiProvider.WEATHER,
        api_name=ApiName.WEATHER,
    )

    workers = []
    for item in worker_repo.get_workers():
        workers.append(
            {
                "name": item.worker_name,
                "status": worker_repo.resolve_display_status(item),
                "last_heartbeat": (
                    item.last_heartbeat.isoformat() if item.last_heartbeat else None
                ),
                "processed_today": item.processed_count or 0,
                "failed_today": item.failed_count or 0,
                "current_job_id": item.current_job_id,
                "version": item.version,
            }
        )

    return {
        "upload": {
            "waiting": upload_repo.count_by_status(UploadJobStatus.WAITING),
            "processing": upload_repo.count_by_status(UploadJobStatus.PROCESSING),
            "failed": upload_repo.count_by_status(UploadJobStatus.FAILED),
            "completed_today": upload_repo.count_completed_today(),
        },
        "vision": {
            "waiting": vision_repo.count_by_status(VisionJobStatus.WAITING),
            "processing": vision_repo.count_by_status(VisionJobStatus.PROCESSING),
            "failed": vision_repo.count_by_status(VisionJobStatus.FAILED),
            "completed_today": vision_repo.count_completed_today(),
        },
        "api_usage": {
            "vision": {
                "used": vision_usage.used_unit or 0,
                "limit": vision_usage.limit_unit or 0,
                "remaining": max(
                    0,
                    (vision_usage.limit_unit or 0) - (vision_usage.used_unit or 0),
                ),
            },
            "geocoding": {
                "used": geocoding_usage.used_unit or 0,
                "limit": geocoding_usage.limit_unit or 0,
            },
            "weather": {
                "used": weather_usage.used_unit or 0,
            },
        },
        "storage": {
            "incoming": _count_files(settings.incoming_dir_path),
            "original": _count_files(settings.original_dir_path),
            "preview": _count_files(settings.preview_dir_path),
            "thumb": _count_files(settings.thumb_dir_path),
        },
        "workers": workers,
    }


def _check_database(db: Session) -> str:
    try:
        db.execute(text("SELECT 1"))
        return "OK"
    except Exception:
        return "FAIL"


def _check_storage() -> str:
    try:
        return "OK" if settings.incoming_dir_path.exists() else "FAIL"
    except Exception:
        return "FAIL"


def _check_vision_credential() -> str:
    path = settings.GOOGLE_VISION_CREDENTIAL
    if not path:
        return "FAIL"
    try:
        return "OK" if Path(path).is_file() else "FAIL"
    except Exception:
        return "FAIL"


def _check_weather_key() -> str:
    return "OK" if settings.WEATHER_API_KEY else "FAIL"


def _check_geocoding_key() -> str:
    return "OK" if settings.GOOGLE_API_KEY else "FAIL"


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file())
