"""Worker heartbeat / status helper."""

from __future__ import annotations

import atexit
import logging
import time

from app.common.database import SessionLocal
from app.common.repositories.worker_status_repository import (
    WorkerStatusRepository,
    WorkerStatusValue,
)

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_INTERVAL = 30
WORKER_VERSION = "1.0.0"


class WorkerMonitor:
    """Worker 시작/종료/Heartbeat/카운터 관리."""

    def __init__(
        self,
        worker_name: str,
        *,
        version: str = WORKER_VERSION,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        self.worker_name = worker_name
        self.version = version
        self.heartbeat_interval = heartbeat_interval
        self._last_heartbeat_at = 0.0
        self._started = False

    def start(self) -> None:
        """Worker를 RUNNING으로 등록한다."""
        db = SessionLocal()
        try:
            WorkerStatusRepository(db).update_status(
                self.worker_name,
                WorkerStatusValue.RUNNING,
                version=self.version,
                clear_current_job=True,
            )
            self._started = True
            self._last_heartbeat_at = time.monotonic()
            atexit.register(self.stop)
            logger.info("Worker monitor started: %s", self.worker_name)
        finally:
            db.close()

    def stop(self) -> None:
        """Worker를 STOPPED로 변경한다."""
        if not self._started:
            return
        db = SessionLocal()
        try:
            WorkerStatusRepository(db).update_status(
                self.worker_name,
                WorkerStatusValue.STOPPED,
                clear_current_job=True,
            )
            self._started = False
            logger.info("Worker monitor stopped: %s", self.worker_name)
        except Exception:
            logger.exception("Failed to mark worker stopped: %s", self.worker_name)
        finally:
            db.close()

    def maybe_heartbeat(self, *, current_job_id: str | None = None) -> None:
        """heartbeat_interval 경과 시 heartbeat를 갱신한다."""
        now = time.monotonic()
        if now - self._last_heartbeat_at < self.heartbeat_interval:
            return
        db = SessionLocal()
        try:
            WorkerStatusRepository(db).heartbeat(
                self.worker_name,
                current_job_id=current_job_id,
                clear_current_job=current_job_id is None,
            )
            self._last_heartbeat_at = now
        except Exception:
            logger.exception("Failed to update heartbeat: %s", self.worker_name)
        finally:
            db.close()

    def mark_processed(self) -> None:
        """성공 처리 건수를 증가시킨다."""
        db = SessionLocal()
        try:
            WorkerStatusRepository(db).increase_processed(self.worker_name)
        except Exception:
            logger.exception("Failed to increase processed: %s", self.worker_name)
        finally:
            db.close()

    def mark_failed(self) -> None:
        """실패 처리 건수를 증가시킨다."""
        db = SessionLocal()
        try:
            WorkerStatusRepository(db).increase_failed(self.worker_name)
        except Exception:
            logger.exception("Failed to increase failed: %s", self.worker_name)
        finally:
            db.close()
