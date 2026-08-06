"""Upload pipeline background worker."""

from __future__ import annotations

import logging
import os
import time

from sqlalchemy.orm import Session

from app.common.database import SessionLocal, initialize_database
from app.common.models.upload_job import UploadJob
from app.common.repositories.upload_job_repository import UploadJobRepository
from app.common.repositories.vision_job_repository import (
    VisionJobRepository,
    VisionJobStatus,
    VisionProvider,
)
from app.common.services.priority_calculator import PriorityCalculator
from app.common.services.storage_service import StorageService
from app.common.utils.perf import Stopwatch, log_perf
from worker.plugins.base import PluginContext
from worker.plugins.plugin_manager import PluginManager
from worker.worker_id import resolve_stale_seconds, resolve_upload_worker_id
from worker.worker_monitor import WorkerMonitor

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

POLLING_INTERVAL = int(os.environ.get("POLLING_INTERVAL", "2"))
STALE_RECOVERY_INTERVAL = int(os.environ.get("UPLOAD_STALE_RECOVERY_INTERVAL", "30"))


def run_worker(poll_interval: int = POLLING_INTERVAL) -> None:
    """업로드 후처리 Worker를 실행한다."""
    initialize_database()
    worker_id = resolve_upload_worker_id()
    stale_seconds = resolve_stale_seconds()
    monitor = WorkerMonitor(worker_id)
    monitor.start()
    logger.info(
        "Upload background worker started worker_id=%s stale_seconds=%s",
        worker_id,
        stale_seconds,
    )

    last_stale_at = 0.0
    # 시작 시 1회 stale 복구
    _recover_stale(worker_id=worker_id, stale_seconds=stale_seconds)
    last_stale_at = time.monotonic()

    try:
        while True:
            monitor.maybe_heartbeat()
            now = time.monotonic()
            if now - last_stale_at >= STALE_RECOVERY_INTERVAL:
                _recover_stale(worker_id=worker_id, stale_seconds=stale_seconds)
                last_stale_at = now

            claimed = False
            try:
                claimed = process_next_job(worker_id=worker_id, monitor=monitor)
            except Exception:
                logger.exception(
                    "Unexpected worker error in main loop worker_id=%s",
                    worker_id,
                )

            if not claimed:
                time.sleep(poll_interval)
    finally:
        monitor.stop()


def _recover_stale(*, worker_id: str, stale_seconds: int) -> int:
    db = SessionLocal()
    try:
        return UploadJobRepository(db).recover_stale_processing_jobs(
            stale_seconds=stale_seconds,
            worker_id=worker_id,
        )
    finally:
        db.close()


def process_next_job(
    *,
    worker_id: str | None = None,
    monitor: WorkerMonitor | None = None,
    db: Session | None = None,
) -> bool:
    """
    다음 WAITING UploadJob을 claim 후 처리한다.

    claim transaction과 processing transaction을 분리한다.
    """
    resolved_worker_id = worker_id or resolve_upload_worker_id()

    # --- claim transaction (short) ---
    claim_db = db if db is not None else SessionLocal()
    owned_claim_db = db is None
    try:
        repository = UploadJobRepository(claim_db)
        job = repository.claim_next_waiting_job(resolved_worker_id)
        job_id = job.job_id if job is not None else None
    finally:
        if owned_claim_db:
            claim_db.close()

    if job_id is None:
        return False

    if monitor is not None:
        monitor.maybe_heartbeat(current_job_id=job_id, force=True)

    # --- processing transaction (separate session) ---
    process_db = SessionLocal()
    try:
        repository = UploadJobRepository(process_db)
        job = repository.get(job_id)
        if job is None or job.status != "PROCESSING":
            logger.warning(
                "Claimed job missing or not PROCESSING job_id=%s status=%s",
                job_id,
                getattr(job, "status", None),
            )
            return True

        try:
            process_upload_job(
                process_db,
                job,
                worker_id=resolved_worker_id,
                monitor=monitor,
            )
            if monitor is not None:
                monitor.mark_processed()
        except Exception as exc:
            log_snapshot = job.processing_log
            error_message = str(exc)
            logger.exception(
                "Upload job failed: job_id=%s worker_id=%s",
                job.job_id,
                resolved_worker_id,
            )
            process_db.rollback()
            fresh = repository.get(job.job_id) or job
            if log_snapshot:
                fresh.processing_log = log_snapshot
            repository.mark_failed(fresh, error_message=error_message)
            if monitor is not None:
                monitor.mark_failed()
    finally:
        process_db.close()
        if monitor is not None:
            monitor.maybe_heartbeat(current_job_id=None, force=True)

    return True


def process_upload_job(
    db: Session,
    job: UploadJob,
    *,
    worker_id: str | None = None,
    monitor: WorkerMonitor | None = None,
) -> None:
    """
    업로드 작업을 처리한다.

    Plugin 목록은 PluginManager.load_plugins()가 Registry에서 자동 로드한다.
    """
    watch = Stopwatch()
    storage_service = StorageService()
    repository = UploadJobRepository(db)
    resolved_worker_id = worker_id or resolve_upload_worker_id()

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
        service_name="MemoryKeeper",
        worker_id=resolved_worker_id,
        worker_monitor=monitor,
    )

    watch.start("plugins")
    PluginManager.load_plugins(worker_scope="upload").run(context)
    plugins_ms = watch.stop("plugins")

    if context.common_file is None:
        raise RuntimeError("Plugin pipeline completed without common_file")

    queue_ms = 0.0
    if not context.stop_pipeline:
        watch.start("vision_queue")
        _enqueue_vision_job(db, context)
        queue_ms = watch.stop("vision_queue")

    completed = repository.mark_completed(job, file_id=context.common_file.file_id)
    if completed is None:
        raise RuntimeError(
            f"Upload job completed by another worker or invalid state: {job.job_id}"
        )
    log_perf(
        "upload_worker_job",
        pipeline="UPLOAD",
        job_id=job.job_id,
        worker_id=resolved_worker_id,
        plugins_ms=plugins_ms,
        vision_queue_ms=queue_ms,
        elapsed_ms=watch.total_ms(),
        note="Vision completion is separate from Upload completion",
    )
    logger.info(
        "Completed upload job_id=%s common_file_id=%s file_id=%s worker_id=%s",
        job.job_id,
        context.common_file.id,
        context.common_file.file_id,
        resolved_worker_id,
    )


def _enqueue_vision_job(db: Session, context: PluginContext) -> None:
    """Metadata/EXIF/GPS 완료 후 Vision Queue를 등록한다."""
    if context.common_file is None:
        return

    vision_repository = VisionJobRepository(db)
    blocking_status = vision_repository.get_blocking_status(
        file_id=context.common_file.id
    )
    if blocking_status in {
        VisionJobStatus.WAITING,
        VisionJobStatus.PROCESSING,
    }:
        context.log("VISION_QUEUE_SKIPPED:ALREADY_EXISTS")
        return

    vision_completed = blocking_status == VisionJobStatus.COMPLETED
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
        skip_duplicate_check=True,
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
