from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.astrojournal.models.plate_solve_job import AstroPlateSolveJob
from app.astrojournal.repositories.plate_solve_job_repository import PlateSolveJobStatus
from app.astrojournal.schemas.observation_record import ObservationRecordCreate
from app.astrojournal.services.observation_record_service import ObservationRecordService
from app.astrojournal.services.plate_solve_queue_service import PlateSolveQueueService
from app.astrojournal.services.plate_solve_service import PlateSolveService
from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from worker.plate_solve_worker import process_next_plate_solve_job


class PlateSolveQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def _file(self, digest: str = "a" * 64) -> CommonFile:
        image_path = Path(self.temp.name) / f"{digest[:8]}.fits"
        image_path.write_bytes(b"fits")
        common_file = CommonFile(
            file_id=digest,
            original_name=image_path.name,
            original_path=str(image_path),
            width=3600,
            height=1800,
            service_name="AstroJournal",
            deleted=False,
        )
        self.db.add(common_file)
        self.db.flush()
        self.db.add(
            CommonFileService(
                file_id=common_file.id,
                service_name="AstroJournal",
            )
        )
        self.db.commit()
        return common_file

    def _record(self, common_file: CommonFile):
        return ObservationRecordService(self.db).create(
            ObservationRecordCreate(
                file_id=common_file.id,
                captured_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            )
        )

    def test_observation_create_enqueues_canonical_common_file_id_once(self) -> None:
        common_file = self._file()
        first = self._record(common_file)
        second = self._record(common_file)

        jobs = self.db.query(AstroPlateSolveJob).all()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].common_file_id, common_file.id)
        self.assertIsInstance(jobs[0].common_file_id, int)
        self.assertEqual(jobs[0].observation_record_id, first.id)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(jobs[0].status, PlateSolveJobStatus.WAITING)
        self.assertEqual(first.plate_solve_status, PlateSolveJobStatus.WAITING)

    def test_post_reuses_existing_queue_job_without_provider_submit(self) -> None:
        common_file = self._file()
        self._record(common_file)
        job = self.db.query(AstroPlateSolveJob).one()

        with patch(
            "app.astrojournal.services.plate_solve_service.AstrometryClient.submit",
            side_effect=AssertionError("legacy provider submit must not run"),
        ):
            response = PlateSolveService(self.db).submit(
                common_file_id=common_file.id
            )
        self.assertEqual(response["job_id"], job.id)
        self.assertEqual(response["common_file_id"], common_file.id)
        self.assertEqual(response["status"], PlateSolveJobStatus.WAITING)

    def test_post_creates_persistent_queue_job_when_missing(self) -> None:
        common_file = self._file()

        with patch(
            "app.astrojournal.services.plate_solve_service.AstrometryClient.submit",
            side_effect=AssertionError("POST must not call the provider"),
        ):
            response = PlateSolveService(self.db).submit(
                common_file_id=common_file.id
            )

        job = self.db.query(AstroPlateSolveJob).one()
        self.assertEqual(response["job_id"], job.id)
        self.assertEqual(job.common_file_id, common_file.id)
        self.assertIsNone(job.observation_record_id)
        self.assertEqual(job.status, PlateSolveJobStatus.WAITING)

    def test_claim_complete_and_completed_job_is_not_recreated(self) -> None:
        common_file = self._file()
        self._record(common_file)
        queue = PlateSolveQueueService(self.db)
        claimed = queue.claim_next(worker_id="test-worker", lease_seconds=60)

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, PlateSolveJobStatus.PROCESSING)
        self.assertEqual(claimed.attempts, 1)
        completed = queue.complete(
            job_id=claimed.id,
            worker_id="test-worker",
            provider={
                "provider_job_id": 99,
                "ra": 10.0,
                "dec": 20.0,
                "rotation": 30.0,
                "pixel_scale": 2.0,
                "field_width": 2.0,
                "field_height": 1.0,
                "parity": 1.0,
            },
        )
        self.assertEqual(completed.status, PlateSolveJobStatus.COMPLETED)

        _, created = queue.enqueue(
            common_file_id=common_file.id,
            observation_record_id=None,
        )
        self.assertFalse(created)
        self.assertEqual(self.db.query(AstroPlateSolveJob).count(), 1)

    def test_failed_retry_preserves_attempt_count(self) -> None:
        common_file = self._file()
        record = self._record(common_file)
        queue = PlateSolveQueueService(self.db)
        claimed = queue.claim_next(worker_id="test-worker", lease_seconds=60)
        queue.record_submission(
            job_id=claimed.id,
            worker_id="test-worker",
            submission_id=41,
        )
        failed = queue.fail(
            job_id=claimed.id,
            worker_id="test-worker",
            error_message="provider failed",
        )
        self.assertEqual(failed.status, PlateSolveJobStatus.FAILED)
        self.assertEqual(failed.attempts, 1)

        retried = queue.retry(failed.id)
        self.assertEqual(retried.status, PlateSolveJobStatus.WAITING)
        self.assertIsNone(retried.provider_submission_id)
        self.db.refresh(record)
        self.assertEqual(record.plate_solve_status, PlateSolveJobStatus.WAITING)
        claimed_again = queue.claim_next(worker_id="test-worker", lease_seconds=60)
        self.assertEqual(claimed_again.attempts, 2)

    def test_worker_provider_failure_marks_job_failed(self) -> None:
        common_file = self._file()
        self._record(common_file)

        class FailedClient:
            def __init__(self, **_kwargs) -> None:
                pass

            def submit(self, *, image_path: str):
                return {"status": "WAITING", "submission_id": 41}

            def get_status(self, *, submission_id: int):
                return {
                    "status": "FAILED",
                    "submission_id": submission_id,
                    "provider_job_id": 99,
                }

            def close(self) -> None:
                pass

        processed = process_next_plate_solve_job(
            session_factory=self.Session,
            client_factory=FailedClient,
            worker_id="test-worker",
            api_key="test-key",
            provider_poll_interval=0,
            provider_timeout=1,
        )
        self.assertTrue(processed)
        job = self.db.query(AstroPlateSolveJob).one()
        self.db.refresh(job)
        self.assertEqual(job.status, PlateSolveJobStatus.FAILED)
        self.assertEqual(job.attempts, 1)
        self.assertIn("FAILED", job.last_error)

    def test_status_summary(self) -> None:
        first_file = self._file("1" * 64)
        second_file = self._file("2" * 64)
        self._record(first_file)
        self._record(second_file)
        queue = PlateSolveQueueService(self.db)
        claimed = queue.claim_next(worker_id="test-worker", lease_seconds=60)
        queue.fail(
            job_id=claimed.id,
            worker_id="test-worker",
            error_message="failed",
        )

        summary = queue.summary()
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["WAITING"], 1)
        self.assertEqual(summary["PROCESSING"], 0)
        self.assertEqual(summary["COMPLETED"], 0)
        self.assertEqual(summary["FAILED"], 1)

    def test_expired_lease_resumes_saved_provider_submission(self) -> None:
        common_file = self._file()
        self._record(common_file)
        queue = PlateSolveQueueService(self.db)
        claimed = queue.claim_next(worker_id="worker-a", lease_seconds=60)
        queue.record_submission(
            job_id=claimed.id,
            worker_id="worker-a",
            submission_id=41,
        )
        claimed.lease_expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.db.commit()

        class ResumeClient:
            seen_submission_id: int | None = None

            def __init__(self, **_kwargs) -> None:
                pass

            def submit(self, *, image_path: str):
                raise AssertionError("saved provider submission must not be resubmitted")

            def get_status(self, *, submission_id: int):
                type(self).seen_submission_id = submission_id
                return {
                    "status": "COMPLETED",
                    "submission_id": submission_id,
                    "provider_job_id": 99,
                    "ra": 10,
                    "dec": 20,
                    "rotation": 30,
                    "pixel_scale": 2,
                    "parity": 1,
                }

            def close(self) -> None:
                pass

        processed = process_next_plate_solve_job(
            session_factory=self.Session,
            client_factory=ResumeClient,
            worker_id="worker-b",
            api_key="test-key",
            provider_poll_interval=0,
            provider_timeout=1,
        )
        self.assertTrue(processed)
        job = self.db.query(AstroPlateSolveJob).one()
        self.db.refresh(job)
        self.assertEqual(job.status, PlateSolveJobStatus.COMPLETED)
        self.assertEqual(job.attempts, 2)
        self.assertEqual(ResumeClient.seen_submission_id, 41)

    def test_provider_calls_run_without_an_open_db_transaction(self) -> None:
        common_file = self._file()
        self._record(common_file)
        opened_sessions: list[Session] = []

        def tracking_session() -> Session:
            session = self.Session()
            opened_sessions.append(session)
            return session

        class FakeClient:
            def __init__(self, **_kwargs) -> None:
                pass

            def submit(self, *, image_path: str):
                self._assert_no_transaction()
                self.assert_image_path = image_path
                return {"status": "WAITING", "submission_id": 41}

            def get_status(self, *, submission_id: int):
                self._assert_no_transaction()
                return {
                    "status": "COMPLETED",
                    "submission_id": submission_id,
                    "provider_job_id": 99,
                    "ra": 10,
                    "dec": 20,
                    "rotation": 30,
                    "pixel_scale": 2,
                    "parity": 1,
                }

            def close(self) -> None:
                pass

            @staticmethod
            def _assert_no_transaction() -> None:
                assert not any(session.in_transaction() for session in opened_sessions)

        processed = process_next_plate_solve_job(
            session_factory=tracking_session,
            client_factory=FakeClient,
            worker_id="test-worker",
            api_key="test-key",
            provider_poll_interval=0,
            provider_timeout=1,
        )
        self.assertTrue(processed)
        job = self.db.query(AstroPlateSolveJob).one()
        self.db.refresh(job)
        self.assertEqual(job.status, PlateSolveJobStatus.COMPLETED)
        self.assertEqual(job.common_file_id, common_file.id)


if __name__ == "__main__":
    unittest.main()
