"""Vision Queue worker."""

from __future__ import annotations

import logging
import os
import time

from sqlalchemy.orm import Session

from app.common.database import Base, SessionLocal, engine
from app.common.models.file import CommonFile
from app.common.models.vision_job import CommonVisionJob
from app.common.repositories.api_usage_repository import (
    ApiName,
    ApiProvider,
    ApiUsageRepository,
)
from app.common.repositories.vision_job_repository import VisionJobRepository
from app.common.services.storage_service import StorageService
from worker.plugins.base import PluginContext
from worker.plugins.plugin_manager import PluginManager
from worker.worker_monitor import WorkerMonitor

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

POLLING_INTERVAL = int(os.environ.get("VISION_POLLING_INTERVAL", "5"))
USAGE_RETRY_INTERVAL = int(os.environ.get("VISION_USAGE_RETRY_INTERVAL", "1800"))
WORKER_NAME = "VisionWorker"


def run_worker(poll_interval: int = POLLING_INTERVAL) -> None:
    """
    Vision Queue Worker를 실행한다.

    WAITING job을 priority DESC / requested_at DESC로 가져와 VisionPlugin을 실행한다.
    Usage limit 초과 시 Worker를 종료하지 않고 WAITING을 유지한 채 재시도한다.
    """
    Base.metadata.create_all(bind=engine)
    monitor = WorkerMonitor(WORKER_NAME)
    monitor.start()
    logger.info("Vision worker started")

    try:
        while True:
            monitor.maybe_heartbeat()
            db = SessionLocal()
            try:
                processed = process_next_vision_job(db, monitor=monitor)
                if not processed:
                    time.sleep(poll_interval)
            except Exception:
                logger.exception("Unexpected vision worker error in main loop")
                time.sleep(poll_interval)
            finally:
                db.close()
    finally:
        monitor.stop()


def process_next_vision_job(
    db: Session,
    *,
    monitor: WorkerMonitor | None = None,
) -> bool:
    """
    다음 WAITING VisionJob 하나를 처리한다.

    Returns:
        bool: 처리했거나 usage limit으로 대기했으면 True, job이 없으면 False
    """
    repository = VisionJobRepository(db)
    job = repository.get_next_waiting_job()
    if job is None:
        return False

    usage_repository = ApiUsageRepository(db)
    if not usage_repository.can_use(
        provider=ApiProvider.GOOGLE,
        api_name=ApiName.VISION,
        units=1,
    ):
        logger.warning(
            "VISION usage limit reached. Keep job WAITING and retry later. job_id=%s",
            job.id,
        )
        _wait_with_heartbeat(
            monitor,
            seconds=USAGE_RETRY_INTERVAL,
            current_job_id=str(job.id),
        )
        return True

    if monitor is not None:
        monitor.maybe_heartbeat(current_job_id=str(job.id))

    repository.mark_processing(job)
    try:
        process_vision_job(db, job)
        repository.mark_completed(job)
        if monitor is not None:
            monitor.mark_processed()
    except Exception as exc:
        logger.exception("Vision job failed: job_id=%s", job.id)
        db.rollback()
        db.refresh(job)
        repository.mark_failed(job, error_message=str(exc))
        if monitor is not None:
            monitor.mark_failed()

    return True


def process_vision_job(db: Session, job: CommonVisionJob) -> None:
    """VisionJob에 대해 VisionPlugin pipeline을 실행한다."""
    common_file = (
        db.query(CommonFile)
        .filter(CommonFile.id == job.file_id)
        .first()
    )
    if common_file is None:
        raise FileNotFoundError(f"CommonFile not found: file_id={job.file_id}")

    storage_service = StorageService()
    original_path = None
    if common_file.original_path:
        original_path = storage_service.resolve_storage_path(common_file.original_path)

    context = PluginContext(
        db=db,
        storage_service=storage_service,
        vision_job=job,
        common_file=common_file,
        file_id=common_file.file_id,
        original_path=original_path,
        storage_path=original_path,
        plugin_enabled={},
    )

    try:
        PluginManager.load_plugins(worker_scope="vision").run(context)
    except Exception:
        if "VISION_FAILED" not in context.processing_log:
            context.log("VISION_FAILED")
        logger.error(
            "Vision job processing failed id=%s file_id=%s log=%s",
            job.id,
            job.file_id,
            context.processing_log,
        )
        raise

    logger.info(
        "Completed vision job id=%s file_id=%s log=%s",
        job.id,
        job.file_id,
        context.processing_log,
    )


def _wait_with_heartbeat(
    monitor: WorkerMonitor | None,
    *,
    seconds: int,
    current_job_id: str | None = None,
) -> None:
    """대기 중에도 heartbeat를 유지한다."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if monitor is not None:
            monitor.maybe_heartbeat(current_job_id=current_job_id)
        time.sleep(min(monitor.heartbeat_interval if monitor else 30, remaining))


if __name__ == "__main__":
    run_worker()
