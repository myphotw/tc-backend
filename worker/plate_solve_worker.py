"""Persistent AstroJournal Plate Solve queue worker."""

from __future__ import annotations

from dataclasses import dataclass
from http.client import RemoteDisconnected
import logging
import os
import socket
import time
from typing import Callable

import requests
from sqlalchemy.orm import Session

from app.astrojournal.services.plate_solve_queue_service import PlateSolveQueueService
from app.common.database import SessionLocal, initialize_database
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.repositories.api_usage_repository import (
    ApiName,
    ApiProvider,
    ApiUsageRepository,
)
from app.common.services.api_clients.astrometry import AstrometryClient
from app.common.services.api_clients.base_client import (
    ApiClientError,
    ExternalApiErrorCode,
)
from app.common.services.key_resolver import ExternalServiceName, KeyResolver
from app.common.services.storage_service import StorageService
from worker.worker_monitor import WorkerMonitor

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

WORKER_NAME = "PlateSolveWorker"
POLLING_INTERVAL = int(os.environ.get("PLATE_SOLVE_POLLING_INTERVAL", "5"))
PROVIDER_POLL_INTERVAL = int(
    os.environ.get("PLATE_SOLVE_PROVIDER_POLL_INTERVAL", "5")
)
PROVIDER_TIMEOUT = int(os.environ.get("PLATE_SOLVE_PROVIDER_TIMEOUT", "1800"))
LEASE_SECONDS = int(os.environ.get("PLATE_SOLVE_LEASE_SECONDS", "300"))


@dataclass(frozen=True)
class PlateSolveWork:
    job_id: str
    common_file_id: int
    submission_id: int | None
    provider_job_id: int | None
    image_path: str
    width: int | None
    height: int | None


def run_worker(poll_interval: int = POLLING_INTERVAL) -> None:
    initialize_database()
    worker_id = resolve_plate_solve_worker_id()
    monitor = WorkerMonitor(worker_id)
    monitor.start()
    logger.info("Plate Solve worker started worker_id=%s", worker_id)
    try:
        while True:
            monitor.maybe_heartbeat()
            try:
                processed = process_next_plate_solve_job(
                    monitor=monitor,
                    worker_id=worker_id,
                )
                if not processed:
                    time.sleep(poll_interval)
            except Exception:
                logger.exception("Unexpected Plate Solve worker error")
                time.sleep(poll_interval)
    finally:
        monitor.stop()


def resolve_plate_solve_worker_id() -> str:
    configured = (os.environ.get("PLATE_SOLVE_WORKER_ID") or "").strip()
    if configured:
        return configured
    host = socket.gethostname().replace(" ", "-") or "host"
    return f"{WORKER_NAME}-{host}-{os.getpid()}"


def process_next_plate_solve_job(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    client_factory=AstrometryClient,
    monitor: WorkerMonitor | None = None,
    worker_id: str = WORKER_NAME,
    lease_seconds: int = LEASE_SECONDS,
    provider_poll_interval: int = PROVIDER_POLL_INTERVAL,
    provider_timeout: int = PROVIDER_TIMEOUT,
    api_key: str | None = None,
) -> bool:
    """Claim and process one job without holding a DB transaction during I/O."""
    claim_db = session_factory()
    try:
        claimed = PlateSolveQueueService(claim_db).claim_next(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if claimed is None:
            return False
        job_id = str(claimed.id)
        common_file_id = int(claimed.common_file_id)
        submission_id = (
            int(claimed.provider_submission_id)
            if claimed.provider_submission_id is not None
            else None
        )
        provider_job_id = (
            int(claimed.provider_job_id)
            if claimed.provider_job_id is not None
            else None
        )
    finally:
        claim_db.close()

    if monitor is not None:
        monitor.maybe_heartbeat(current_job_id=job_id, force=True)

    client = None
    try:
        work = _load_work(
            session_factory,
            job_id=job_id,
            common_file_id=common_file_id,
            submission_id=submission_id,
            provider_job_id=provider_job_id,
        )
        resolved_key = api_key or _resolve_api_key(session_factory)
        if not resolved_key:
            raise ApiClientError(
                "ASTROMETRY is not configured",
                code=ExternalApiErrorCode.API_KEY_NOT_CONFIGURED,
            )
        client = client_factory(api_key=resolved_key, db=None)

        if work.submission_id is None:
            _reserve_submit_usage(session_factory)
            submitted = client.submit(image_path=work.image_path)
            submission_id = int(submitted["submission_id"])
            _record_submission(
                session_factory,
                job_id=job_id,
                worker_id=worker_id,
                submission_id=submission_id,
            )
        else:
            submission_id = work.submission_id

        provider = _poll_provider(
            client,
            submission_id=submission_id,
            provider_job_id=work.provider_job_id,
            job_id=job_id,
            worker_id=worker_id,
            session_factory=session_factory,
            monitor=monitor,
            lease_seconds=lease_seconds,
            poll_interval=provider_poll_interval,
            timeout=provider_timeout,
        )
        provider = _normalize_result(provider, width=work.width, height=work.height)
        result_db = session_factory()
        try:
            PlateSolveQueueService(result_db).complete(
                job_id=job_id,
                worker_id=worker_id,
                provider=provider,
            )
        finally:
            result_db.close()
        if monitor is not None:
            monitor.mark_processed()
        logger.info(
            "Completed Plate Solve job_id=%s common_file_id=%s",
            job_id,
            common_file_id,
        )
    except Exception as exc:
        if submission_id is not None and _is_transient_provider_error(exc):
            logger.warning(
                "Transient Plate Solve provider error; requeueing job_id=%s "
                "submission_id=%s",
                job_id,
                submission_id,
                exc_info=True,
            )
            retry_db = session_factory()
            try:
                PlateSolveQueueService(retry_db).requeue_transient(
                    job_id=job_id,
                    worker_id=worker_id,
                    error_message=str(exc)[:4000],
                )
            finally:
                retry_db.close()
            if monitor is not None:
                monitor.maybe_heartbeat(current_job_id=None, force=True)
        else:
            logger.exception("Plate Solve job failed job_id=%s", job_id)
            failure_db = session_factory()
            try:
                PlateSolveQueueService(failure_db).fail(
                    job_id=job_id,
                    worker_id=worker_id,
                    error_message=str(exc)[:4000],
                )
            finally:
                failure_db.close()
            if monitor is not None:
                monitor.mark_failed()
    finally:
        if client is not None:
            client.close()
    return True


def _load_work(
    session_factory: Callable[[], Session],
    *,
    job_id: str,
    common_file_id: int,
    submission_id: int | None,
    provider_job_id: int | None,
) -> PlateSolveWork:
    db = session_factory()
    try:
        common_file = (
            db.query(CommonFile)
            .join(
                CommonFileService,
                (CommonFileService.file_id == CommonFile.id)
                & (CommonFileService.service_name == "AstroJournal"),
            )
            .filter(CommonFile.id == common_file_id)
            .filter(CommonFile.deleted.is_(False))
            .first()
        )
        if common_file is None or not common_file.original_path:
            raise FileNotFoundError(
                f"AstroJournal original file not found: common_file_id={common_file_id}"
            )
        original_path = str(common_file.original_path)
        width = common_file.width
        height = common_file.height
    finally:
        db.close()

    # NAS filesystem access happens only after the read transaction is closed.
    image_path = StorageService().resolve_storage_path(original_path)
    if not image_path.is_file():
        raise FileNotFoundError(
            f"Plate Solve source file not found: common_file_id={common_file_id}"
        )
    return PlateSolveWork(
        job_id=job_id,
        common_file_id=common_file_id,
        submission_id=submission_id,
        provider_job_id=provider_job_id,
        image_path=str(image_path),
        width=width,
        height=height,
    )


def _resolve_api_key(session_factory: Callable[[], Session]) -> str | None:
    db = session_factory()
    try:
        return KeyResolver(db).resolve(ExternalServiceName.ASTROMETRY)
    finally:
        db.close()


def _reserve_submit_usage(session_factory: Callable[[], Session]) -> None:
    db = session_factory()
    try:
        reserved = ApiUsageRepository(db).reserve_usage(
            provider=ApiProvider.ASTROMETRY,
            api_name=ApiName.PLATESOLVE,
            units=1,
        )
        if not reserved:
            raise ApiClientError(
                "Plate Solve monthly limit exceeded",
                code=ExternalApiErrorCode.API_LIMIT_EXCEEDED,
            )
    finally:
        db.close()


def _record_submission(
    session_factory: Callable[[], Session],
    *,
    job_id: str,
    worker_id: str,
    submission_id: int,
) -> None:
    db = session_factory()
    try:
        PlateSolveQueueService(db).record_submission(
            job_id=job_id,
            worker_id=worker_id,
            submission_id=submission_id,
        )
    finally:
        db.close()


def _record_provider_job(
    session_factory: Callable[[], Session],
    *,
    job_id: str,
    worker_id: str,
    provider_job_id: int,
) -> None:
    db = session_factory()
    try:
        PlateSolveQueueService(db).record_provider_job(
            job_id=job_id,
            worker_id=worker_id,
            provider_job_id=provider_job_id,
        )
    finally:
        db.close()


def _touch_lease(
    session_factory: Callable[[], Session],
    *,
    job_id: str,
    worker_id: str,
    lease_seconds: int,
) -> None:
    db = session_factory()
    try:
        touched = PlateSolveQueueService(db).touch_lease(
            job_id=job_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if not touched:
            raise RuntimeError("Plate Solve job lease was lost")
    finally:
        db.close()


def _poll_provider(
    client,
    *,
    submission_id: int,
    provider_job_id: int | None,
    job_id: str,
    worker_id: str,
    session_factory: Callable[[], Session],
    monitor: WorkerMonitor | None,
    lease_seconds: int,
    poll_interval: int,
    timeout: int,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while True:
        _touch_lease(
            session_factory,
            job_id=job_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if monitor is not None:
            monitor.maybe_heartbeat(current_job_id=job_id)
        if provider_job_id is None:
            provider = client.get_submission_status(submission_id=submission_id)
            status = provider.get("status")
            if status == "FAILED":
                raise RuntimeError("Astrometry provider reported FAILED")
            resolved_job_id = provider.get("provider_job_id")
            if resolved_job_id is not None:
                provider_job_id = int(resolved_job_id)
                _record_provider_job(
                    session_factory,
                    job_id=job_id,
                    worker_id=worker_id,
                    provider_job_id=provider_job_id,
                )
                continue
        else:
            provider = client.get_job_status(
                submission_id=submission_id,
                provider_job_id=provider_job_id,
            )
        status = provider.get("status")
        if status == "COMPLETED":
            return provider
        if status == "FAILED":
            raise RuntimeError("Astrometry provider reported FAILED")
        if time.monotonic() >= deadline:
            raise ApiClientError(
                "Astrometry provider timed out",
                code=ExternalApiErrorCode.PROVIDER_TIMEOUT,
            )
        time.sleep(poll_interval)


def _is_transient_provider_error(exc: BaseException) -> bool:
    """Return whether a provider lookup failure is safe to resume later."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current,
            (requests.Timeout, requests.ConnectionError, RemoteDisconnected),
        ):
            return True
        if isinstance(current, ApiClientError):
            if current.code == ExternalApiErrorCode.PROVIDER_TIMEOUT:
                return True
            status_code = current.status_code
            if status_code in {408, 429} or (
                status_code is not None and status_code >= 500
            ):
                return True
            if current.code == ExternalApiErrorCode.PROVIDER_ERROR and (
                status_code is None or status_code < 400
            ):
                return True
        current = current.__cause__ or current.__context__
    return False


def _normalize_result(
    provider: dict[str, object],
    *,
    width: int | None,
    height: int | None,
) -> dict[str, object]:
    normalized = dict(provider)
    pixel_scale = provider.get("pixel_scale")
    if pixel_scale is not None and width:
        normalized["field_width"] = float(pixel_scale) * width / 3600.0
    if pixel_scale is not None and height:
        normalized["field_height"] = float(pixel_scale) * height / 3600.0
    return normalized


if __name__ == "__main__":
    run_worker()
