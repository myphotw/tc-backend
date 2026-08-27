from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.upload_job import UploadJob
from app.common.repositories.upload_job_repository import (
    UploadJobRepository,
    UploadJobStatus,
)
from worker.plugins.base import PluginContext
from worker.worker_monitor import WorkerMonitor


class UploadWorkerHeartbeatTests(unittest.TestCase):
    def test_plugin_boundary_updates_worker_status_without_touching_locked_job(self) -> None:
        """10초 지난 Job의 row lock을 별도 lease UPDATE로 기다리지 않는다."""
        worker_heartbeats: list[tuple[str, str | None]] = []
        lease_touch_started = threading.Event()
        release_row_lock = threading.Event()

        class FakeSession:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class RecordingWorkerStatusRepository:
            def __init__(self, db: FakeSession) -> None:
                self.db = db

            def heartbeat(
                self,
                worker_name: str,
                *,
                current_job_id: str | None = None,
                clear_current_job: bool = False,
            ) -> None:
                worker_heartbeats.append((worker_name, current_job_id))

        class LockedUploadJobRepository:
            def __init__(self, db: FakeSession) -> None:
                self.db = db

            def touch_processing_lease(self, *, job_id: str) -> None:
                lease_touch_started.set()
                release_row_lock.wait(timeout=10)

        monitor = WorkerMonitor(
            "upload-worker-test",
            heartbeat_interval=30,
            job_heartbeat_interval=10,
        )
        monitor._last_heartbeat_at = 100.0
        monitor._last_job_touch_at = 100.0
        heartbeat_session = FakeSession()
        context = PluginContext(
            db=SimpleNamespace(),
            storage_service=SimpleNamespace(),
            job=SimpleNamespace(job_id="locked-upload-job"),
            worker_monitor=monitor,
        )

        with patch(
            "worker.worker_monitor.SessionLocal",
            return_value=heartbeat_session,
        ), patch(
            "worker.worker_monitor.WorkerStatusRepository",
            RecordingWorkerStatusRepository,
        ), patch(
            "worker.worker_monitor.UploadJobRepository",
            LockedUploadJobRepository,
        ), patch(
            "worker.worker_monitor.time.monotonic",
            return_value=111.0,
        ):
            boundary_thread = threading.Thread(
                target=context.notify_plugin_boundary,
                daemon=True,
            )
            boundary_thread.start()
            boundary_thread.join(timeout=0.5)

        release_row_lock.set()
        self.assertFalse(boundary_thread.is_alive())
        self.assertFalse(lease_touch_started.is_set())
        self.assertEqual(
            worker_heartbeats,
            [("upload-worker-test", "locked-upload-job")],
        )
        self.assertTrue(heartbeat_session.closed)


class UploadJobStateRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.repository = UploadJobRepository(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _waiting_job(self, job_id: str) -> UploadJob:
        job = UploadJob(
            job_id=job_id,
            source_type="UPLOAD",
            status=UploadJobStatus.WAITING,
            incoming_path=f"incoming/{job_id}.jpg",
            service_name="MemoryKeeper",
        )
        self.db.add(job)
        self.db.commit()
        return job

    def test_claim_and_completed_state_transitions_are_unchanged(self) -> None:
        job = self._waiting_job("heartbeat-completed-job")

        claimed = self.repository.claim_next_waiting_job("upload-worker-test")
        completed = self.repository.mark_completed(claimed, file_id="a" * 64)

        self.assertEqual(claimed.job_id, job.job_id)
        self.assertEqual(completed.status, UploadJobStatus.COMPLETED)
        self.assertEqual(completed.file_id, "a" * 64)

    def test_claim_and_failed_state_transitions_are_unchanged(self) -> None:
        job = self._waiting_job("heartbeat-failed-job")

        claimed = self.repository.claim_next_waiting_job("upload-worker-test")
        failed = self.repository.mark_failed(claimed, error_message="pipeline failed")

        self.assertEqual(claimed.job_id, job.job_id)
        self.assertEqual(failed.status, UploadJobStatus.FAILED)
        self.assertEqual(failed.error_message, "pipeline failed")
        self.assertEqual(failed.retry_count, 1)


if __name__ == "__main__":
    unittest.main()
