"""Health / Dashboard 모니터링 서비스."""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


def check_health(db: Session) -> dict[str, str]:
    """운영 컴포넌트 Health Check 결과를 반환한다."""
    database, database_detail = _check_database(db)
    storage, storage_detail = _check_storage()
    vision, vision_detail = _check_vision_credential()
    weather, weather_detail = _check_weather_key()
    geocoding, geocoding_detail = _check_geocoding_key()

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
        "version": settings.VERSION,
        **components,
        "database_detail": database_detail,
        "storage_detail": storage_detail,
        "vision_detail": vision_detail,
        "weather_detail": weather_detail,
        "geocoding_detail": geocoding_detail,
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
                "last_started": (
                    item.last_started.isoformat() if item.last_started else None
                ),
                "last_heartbeat": (
                    item.last_heartbeat.isoformat() if item.last_heartbeat else None
                ),
                "processed_today": item.processed_count or 0,
                "failed_today": item.failed_count or 0,
                "current_job_id": item.current_job_id,
                "version": item.version or settings.VERSION,
            }
        )

    return {
        "version": settings.VERSION,
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


def _check_database(db: Session) -> tuple[str, str]:
    try:
        db.execute(text("SELECT 1"))
        return "OK", "database connection ok"
    except Exception as exc:
        logger.exception("Health database check failed")
        return "FAIL", f"{type(exc).__name__}: {exc}"


def _check_storage() -> tuple[str, str]:
    try:
        path = settings.incoming_dir_path
        if path.exists():
            return "OK", f"incoming exists: {path}"
        return "FAIL", f"incoming directory not found: {path}"
    except Exception as exc:
        logger.exception("Health storage check failed")
        return "FAIL", f"{type(exc).__name__}: {exc}"


def _check_vision_credential() -> tuple[str, str]:
    path = settings.GOOGLE_VISION_CREDENTIAL
    if not path:
        detail = "GOOGLE_VISION_CREDENTIAL is not configured"
        logger.error("Health vision check failed: %s", detail)
        return "FAIL", detail
    try:
        credential = Path(path)
        if credential.is_file():
            return "OK", f"credential file exists: {credential}"
        detail = f"GOOGLE_VISION_CREDENTIAL file not found: {credential}"
        logger.error("Health vision check failed: %s", detail)
        return "FAIL", detail
    except Exception as exc:
        logger.exception("Health vision check failed")
        return "FAIL", f"{type(exc).__name__}: {exc}"


def _check_weather_key() -> tuple[str, str]:
    if settings.WEATHER_API_KEY:
        return "OK", "WEATHER_API_KEY is configured"
    detail = "WEATHER_API_KEY is not configured"
    logger.error("Health weather check failed: %s", detail)
    return "FAIL", detail


def _check_geocoding_key() -> tuple[str, str]:
    if settings.GOOGLE_API_KEY:
        return "OK", "GOOGLE_API_KEY is configured"
    detail = (
        "GOOGLE_API_KEY is not configured "
        "(alias GOOGLE_MAP_API_KEY is also accepted via Settings)"
    )
    logger.error("Health geocoding check failed: %s", detail)
    return "FAIL", detail


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file())
