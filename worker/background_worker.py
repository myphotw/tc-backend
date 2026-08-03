"""Upload pipeline background worker."""

from __future__ import annotations

import logging
import os
import time

from sqlalchemy.orm import Session

from app.common.database import Base, SessionLocal, engine
from app.common.models.upload_job import UploadJob
from app.common.repositories.upload_job_repository import UploadJobRepository
from app.common.repositories.vision_job_repository import (
    VisionJobRepository,
    VisionProvider,
)
from app.common.services.priority_calculator import PriorityCalculator
from app.common.services.storage_service import StorageService
from worker.plugins.base import PluginContext
from worker.plugins.plugin_manager import PluginManager
from worker.worker_monitor import WorkerMonitor

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

POLLING_INTERVAL = int(os.environ.get("POLLING_INTERVAL", "2"))
WORKER_NAME = "UploadWorker"


def run_worker(poll_interval: int = POLLING_INTERVAL) -> None:
    """업로드 후처리 Worker를 실행한다."""
    Base.metadata.create_all(bind=engine)
    monitor = WorkerMonitor(WORKER_NAME)
    monitor.start()
    logger.info("Upload background worker started")

    try:
        while True:
            monitor.maybe_heartbeat()
            db = SessionLocal()
            try:
                _ = process_next_job(db, monitor=monitor)
            except Exception:
                logger.exception("Unexpected worker error in main loop")
            finally:
                db.close()

            time.sleep(poll_interval)
    finally:
        monitor.stop()


def process_next_job(
    db: Session,
    *,
    monitor: WorkerMonitor | None = None,
) -> bool:
    """다음 WAITING UploadJob 하나를 처리한다."""
    repository = UploadJobRepository(db)
    job = repository.get_next_waiting_job()
    if job is None:
        return False

    if monitor is not None:
        monitor.maybe_heartbeat(current_job_id=job.job_id)

    repository.mark_processing(job)
    try:
        process_upload_job(db, job)
        if monitor is not None:
            monitor.mark_processed()
    except Exception as exc:
        db.rollback()
        logger.exception("Upload job failed: job_id=%s", job.job_id)
        repository.mark_failed(job, error_message=str(exc))
        if monitor is not None:
            monitor.mark_failed()

    return True


def process_upload_job(db: Session, job: UploadJob) -> None:
    """
    업로드 작업을 처리한다.

    Plugin 목록은 PluginManager.load_plugins()가 Registry에서 자동 로드한다.
    """
    storage_service = StorageService()
    repository = UploadJobRepository(db)

    incoming_path = storage_service.resolve_storage_path(job.incoming_path)
    extension = incoming_path.suffix.lower()
    context = PluginContext(
        db=db,
        job=job,
        storage_service=storage_service,
        job_repository=repository,
        incoming_path=incoming_path,
        original_name=_extract_original_name(incoming_path.name, job.job_id),
        extension=extension,
        mime_type=_guess_mime_type(extension),
        plugin_enabled={},
    )

    PluginManager.load_plugins(worker_scope="upload").run(context)

    if context.common_file is None:
        raise RuntimeError("Plugin pipeline completed without common_file")

    if not context.stop_pipeline:
        _enqueue_vision_job(db, context)

    repository.mark_completed(job, file_id=context.common_file.file_id)
    logger.info(
        "Completed upload job_id=%s common_file_id=%s file_id=%s",
        job.job_id,
        context.common_file.id,
        context.common_file.file_id,
    )


def _enqueue_vision_job(db: Session, context: PluginContext) -> None:
    """Metadata/EXIF/GPS 완료 후 Vision Queue를 등록한다."""
    if context.common_file is None:
        return

    vision_repository = VisionJobRepository(db)
    vision_completed = vision_repository.is_vision_completed(
        file_id=context.common_file.id
    )
    priority = PriorityCalculator().calculate(
        uploaded_at=context.common_file.created_at,
        has_gps=context.has_gps,
        is_favorite=False,
        vision_completed=vision_completed,
    )
    if priority is None:
        context.log("VISION_QUEUE_SKIPPED:VISION_COMPLETED")
        return

    vision_job = vision_repository.create(
        file_id=context.common_file.id,
        priority=priority,
        vision_provider=VisionProvider.GOOGLE,
    )
    if vision_job is None:
        context.log("VISION_QUEUE_SKIPPED:ALREADY_EXISTS")
        return

    context.log(f"VISION_QUEUE_CREATED:priority={priority}")
    logger.info(
        "Created vision job id=%s file_id=%s priority=%s",
        vision_job.id,
        vision_job.file_id,
        vision_job.priority,
    )


def _extract_original_name(incoming_name: str, job_id: str) -> str:
    """incoming 파일명에서 업로드 원본 파일명을 복원한다."""
    prefix = f"{job_id}_"
    if incoming_name.startswith(prefix):
        return incoming_name[len(prefix):]
    return incoming_name


def _guess_mime_type(extension: str) -> str | None:
    """확장자 기반 MIME type을 추정한다."""
    import mimetypes

    mime_type, _ = mimetypes.guess_type(f"file{extension}")
    return mime_type


if __name__ == "__main__":
    run_worker()
