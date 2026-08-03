from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.common.models.worker_status import CommonWorkerStatus


class WorkerStatusValue:
    """Worker 상태 값."""

    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    OFFLINE = "OFFLINE"


class WorkerStatusRepository:
    """common_worker_status 저장소."""

    OFFLINE_THRESHOLD_SECONDS = 60

    def __init__(self, db: Session) -> None:
        self.db = db

    def update_status(
        self,
        worker_name: str,
        status: str,
        *,
        version: str | None = None,
        current_job_id: str | None = None,
        clear_current_job: bool = False,
    ) -> CommonWorkerStatus:
        """Worker 상태를 갱신한다."""
        item = self._get_or_create(worker_name)
        now = datetime.now(timezone.utc)
        item.status = status
        if status == WorkerStatusValue.RUNNING:
            item.last_started = now
            item.last_heartbeat = now
            self._reset_daily_counters_if_needed(item, now=now)
        if version is not None:
            item.version = version
        if clear_current_job:
            item.current_job_id = None
        elif current_job_id is not None:
            item.current_job_id = current_job_id
        item.updated_at = now
        self.db.commit()
        self.db.refresh(item)
        return item

    def heartbeat(
        self,
        worker_name: str,
        *,
        current_job_id: str | None = None,
        clear_current_job: bool = False,
    ) -> CommonWorkerStatus:
        """Worker heartbeat를 갱신한다."""
        item = self._get_or_create(worker_name)
        now = datetime.now(timezone.utc)
        item.status = WorkerStatusValue.RUNNING
        item.last_heartbeat = now
        self._reset_daily_counters_if_needed(item, now=now)
        if clear_current_job:
            item.current_job_id = None
        elif current_job_id is not None:
            item.current_job_id = current_job_id
        item.updated_at = now
        self.db.commit()
        self.db.refresh(item)
        return item

    def increase_processed(self, worker_name: str) -> CommonWorkerStatus:
        """처리 성공 건수를 증가시킨다."""
        item = self._get_or_create(worker_name)
        now = datetime.now(timezone.utc)
        self._reset_daily_counters_if_needed(item, now=now)
        item.processed_count = (item.processed_count or 0) + 1
        item.current_job_id = None
        item.updated_at = now
        self.db.commit()
        self.db.refresh(item)
        return item

    def increase_failed(self, worker_name: str) -> CommonWorkerStatus:
        """처리 실패 건수를 증가시킨다."""
        item = self._get_or_create(worker_name)
        now = datetime.now(timezone.utc)
        self._reset_daily_counters_if_needed(item, now=now)
        item.failed_count = (item.failed_count or 0) + 1
        item.current_job_id = None
        item.updated_at = now
        self.db.commit()
        self.db.refresh(item)
        return item

    def get_workers(self) -> list[CommonWorkerStatus]:
        """등록된 Worker 목록을 반환한다."""
        return (
            self.db.query(CommonWorkerStatus)
            .order_by(CommonWorkerStatus.worker_name.asc())
            .all()
        )

    def get_worker(self, worker_name: str) -> CommonWorkerStatus | None:
        """단일 Worker 상태를 조회한다."""
        return (
            self.db.query(CommonWorkerStatus)
            .filter(CommonWorkerStatus.worker_name == worker_name)
            .first()
        )

    def resolve_display_status(self, item: CommonWorkerStatus) -> str:
        """
        Dashboard 표시용 상태를 반환한다.

        RUNNING이어도 heartbeat가 60초 이상 없으면 OFFLINE이다.
        """
        if item.status == WorkerStatusValue.STOPPED:
            return WorkerStatusValue.STOPPED
        if item.last_heartbeat is None:
            return WorkerStatusValue.OFFLINE

        heartbeat = item.last_heartbeat
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
        if age > self.OFFLINE_THRESHOLD_SECONDS:
            return WorkerStatusValue.OFFLINE
        return item.status or WorkerStatusValue.OFFLINE

    def _get_or_create(self, worker_name: str) -> CommonWorkerStatus:
        item = self.get_worker(worker_name)
        if item is not None:
            return item

        item = CommonWorkerStatus(
            worker_name=worker_name,
            status=WorkerStatusValue.STOPPED,
            processed_count=0,
            failed_count=0,
        )
        self.db.add(item)
        self.db.flush()
        return item

    @staticmethod
    def _reset_daily_counters_if_needed(
        item: CommonWorkerStatus,
        *,
        now: datetime,
    ) -> None:
        """날짜가 바뀌면 일일 카운터를 초기화한다."""
        marker = item.last_heartbeat
        if marker is None:
            return
        if marker.tzinfo is None:
            marker = marker.replace(tzinfo=timezone.utc)
        if marker.date() != now.date():
            item.processed_count = 0
            item.failed_count = 0
