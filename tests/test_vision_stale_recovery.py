from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock, call, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.api_usage import CommonApiUsage
from app.common.models.file import CommonFile
from app.common.models.vision_job import CommonVisionJob
from app.common.models.worker_status import CommonWorkerStatus
from app.common.repositories.api_usage_repository import ApiName, ApiProvider
from app.common.repositories.vision_job_repository import (
    VisionJobRepository,
    VisionJobStatus,
)
from worker.vision_worker import (
    LIVE_HEARTBEAT_SECONDS,
    STALE_RECOVERY_INTERVAL,
    STALE_SECONDS,
    process_next_vision_job,
    run_worker,
)


class VisionStaleRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.repository = VisionJobRepository(self.db)
        self.counter = 0

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _job(
        self,
        status: str,
        *,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        deleted: bool = False,
        retry_count: int = 0,
        priority: int = 0,
        last_error: str | None = None,
    ) -> CommonVisionJob:
        self.counter += 1
        common_file = CommonFile(
            file_id=f"{self.counter:064x}",
            original_name=f"{self.counter}.jpg",
            deleted=False,
        )
        self.db.add(common_file)
        self.db.flush()
        job = CommonVisionJob(
            file_id=common_file.id,
            priority=priority,
            status=status,
            retry_count=retry_count,
            vision_provider="GOOGLE",
            requested_at=datetime.now(timezone.utc),
            started_at=started_at,
            completed_at=completed_at,
            last_error=last_error,
            deleted=deleted,
        )
        self.db.add(job)
        self.db.commit()
        return job

    def _recover(self) -> int:
        return self.repository.recover_stale_processing_jobs(
            stale_seconds=6 * 60 * 60,
            worker_name_prefix="VisionWorker",
            live_heartbeat_seconds=90,
        )

    def test_only_abandoned_processing_jobs_are_requeued_and_claimable(self) -> None:
        now = datetime.now(timezone.utc)
        stale = self._job(
            VisionJobStatus.PROCESSING,
            started_at=now - timedelta(hours=7),
            retry_count=2,
            priority=100,
            last_error="interrupted",
        )
        recent = self._job(
            VisionJobStatus.PROCESSING,
            started_at=now - timedelta(hours=5),
        )
        missing_start = self._job(VisionJobStatus.PROCESSING)
        already_completed_processing = self._job(
            VisionJobStatus.PROCESSING,
            started_at=now - timedelta(hours=8),
            completed_at=now - timedelta(hours=7),
        )
        quota_waiting = self._job(VisionJobStatus.WAITING, priority=1)
        completed = self._job(
            VisionJobStatus.COMPLETED,
            started_at=now - timedelta(hours=8),
            completed_at=now - timedelta(hours=7),
        )
        failed = self._job(
            VisionJobStatus.FAILED,
            started_at=now - timedelta(hours=8),
            completed_at=now - timedelta(hours=7),
            retry_count=1,
            last_error="provider failed",
        )
        deleted = self._job(
            VisionJobStatus.PROCESSING,
            started_at=now - timedelta(hours=8),
            deleted=True,
        )
        self.db.add(
            CommonApiUsage(
                provider=ApiProvider.GOOGLE,
                api_name=ApiName.VISION,
                year=now.year,
                month=now.month,
                used_unit=900,
                limit_unit=900,
                remaining_unit=0,
                deleted=False,
            )
        )
        self.db.commit()

        recovered = self._recover()

        self.assertEqual(recovered, 1)
        for job in (
            stale,
            recent,
            missing_start,
            already_completed_processing,
            quota_waiting,
            completed,
            failed,
            deleted,
        ):
            self.db.refresh(job)
        self.assertEqual(stale.status, VisionJobStatus.WAITING)
        self.assertIsNone(stale.started_at)
        self.assertIsNone(stale.completed_at)
        self.assertIsNone(stale.last_error)
        self.assertEqual(stale.retry_count, 2)
        self.assertEqual(recent.status, VisionJobStatus.PROCESSING)
        self.assertEqual(missing_start.status, VisionJobStatus.PROCESSING)
        self.assertEqual(
            already_completed_processing.status,
            VisionJobStatus.PROCESSING,
        )
        self.assertEqual(quota_waiting.status, VisionJobStatus.WAITING)
        self.assertEqual(quota_waiting.retry_count, 0)
        self.assertEqual(completed.status, VisionJobStatus.COMPLETED)
        self.assertEqual(failed.status, VisionJobStatus.FAILED)
        self.assertTrue(deleted.deleted)

        usage = self.db.query(CommonApiUsage).one()
        self.assertEqual((usage.used_unit, usage.remaining_unit), (900, 0))

        claimed = self.repository.mark_processing(
            self.repository.get_next_waiting_job()
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, stale.id)

    def test_recent_live_worker_current_job_is_not_recovered(self) -> None:
        now = datetime.now(timezone.utc)
        stale = self._job(
            VisionJobStatus.PROCESSING,
            started_at=now - timedelta(hours=7),
        )
        worker = CommonWorkerStatus(
            worker_name="VisionWorker",
            status="RUNNING",
            last_heartbeat=now,
            current_job_id=str(stale.id),
            processed_count=0,
            failed_count=0,
        )
        self.db.add(worker)
        self.db.commit()

        self.assertEqual(self._recover(), 0)
        self.db.refresh(stale)
        self.assertEqual(stale.status, VisionJobStatus.PROCESSING)

        worker.last_heartbeat = now - timedelta(seconds=91)
        self.db.commit()
        self.assertEqual(self._recover(), 1)
        self.db.refresh(stale)
        self.assertEqual(stale.status, VisionJobStatus.WAITING)

    def test_claim_forces_current_job_heartbeat_after_processing_transition(self) -> None:
        job = self._job(VisionJobStatus.WAITING)
        monitor = MagicMock()

        with (
            patch(
                "worker.vision_worker.ApiUsageRepository.can_use",
                return_value=True,
            ),
            patch("worker.vision_worker.process_vision_job"),
        ):
            processed = process_next_vision_job(self.db, monitor=monitor)

        self.assertTrue(processed)
        monitor.maybe_heartbeat.assert_called_once_with(
            current_job_id=str(job.id),
            force=True,
        )
        monitor.mark_processed.assert_called_once_with()

    def test_run_worker_recovers_at_startup_and_periodically(self) -> None:
        monitor = MagicMock()
        loop_session = MagicMock()

        with (
            patch("worker.vision_worker.initialize_database"),
            patch("worker.vision_worker.WorkerMonitor", return_value=monitor),
            patch("worker.vision_worker.SessionLocal", return_value=loop_session),
            patch("worker.vision_worker._recover_stale", return_value=0) as recover,
            patch(
                "worker.vision_worker.time.monotonic",
                side_effect=[100.0, 100.0 + STALE_RECOVERY_INTERVAL],
            ),
            patch(
                "worker.vision_worker.process_next_vision_job",
                side_effect=KeyboardInterrupt,
            ),
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_worker(poll_interval=0)

        self.assertEqual(
            recover.call_args_list,
            [
                call(stale_seconds=STALE_SECONDS),
                call(stale_seconds=STALE_SECONDS),
            ],
        )
        monitor.start.assert_called_once_with()
        monitor.stop.assert_called_once_with()
        loop_session.close.assert_called_once_with()
        self.assertEqual(LIVE_HEARTBEAT_SECONDS, 90)


if __name__ == "__main__":
    unittest.main()
