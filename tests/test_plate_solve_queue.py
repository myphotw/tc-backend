from __future__ import annotations

from datetime import datetime, timezone
from http.client import RemoteDisconnected
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests
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
from app.common.services.api_clients.astrometry import AstrometryProviderWorkNotFound
from app.common.services.api_clients.base_client import ApiClientError
from worker.plate_solve_worker import (
    _is_transient_provider_error,
    process_next_plate_solve_job,
)


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
        queue.record_provider_job(
            job_id=claimed.id,
            worker_id="test-worker",
            provider_job_id=99,
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
        self.assertEqual(retried.provider_submission_id, 41)
        self.assertEqual(retried.provider_job_id, 99)
        self.db.refresh(record)
        self.assertEqual(record.plate_solve_status, PlateSolveJobStatus.WAITING)
        claimed_again = queue.claim_next(worker_id="test-worker", lease_seconds=60)
        self.assertEqual(claimed_again.attempts, 1)

    def test_failed_retry_resumes_saved_provider_job_without_upload(self) -> None:
        common_file = self._file()
        self._record(common_file)
        queue = PlateSolveQueueService(self.db)
        claimed = queue.claim_next(worker_id="setup-worker", lease_seconds=60)
        queue.record_submission(
            job_id=claimed.id,
            worker_id="setup-worker",
            submission_id=15936182,
        )
        queue.record_provider_job(
            job_id=claimed.id,
            worker_id="setup-worker",
            provider_job_id=16771675,
        )
        failed = queue.fail(
            job_id=claimed.id,
            worker_id="setup-worker",
            error_message="temporary operator retry",
        )

        response = PlateSolveService(self.db).retry(job_id=failed.id)
        self.assertEqual(response["provider_metadata"]["submission_id"], 15936182)
        self.assertEqual(response["provider_metadata"]["provider_job_id"], 16771675)

        class ResumeJobClient:
            def __init__(self, **_kwargs) -> None:
                pass

            def submit(self, *, image_path: str):
                raise AssertionError("retry must not create a new upload")

            def get_submission_status(self, *, submission_id: int):
                raise AssertionError("known provider job must be polled directly")

            def get_job_status(self, *, submission_id: int, provider_job_id: int):
                return {
                    "status": "COMPLETED",
                    "submission_id": submission_id,
                    "provider_job_id": provider_job_id,
                    "ra": 10,
                    "dec": 20,
                    "rotation": 30,
                    "pixel_scale": 2,
                    "parity": 1,
                }

            def close(self) -> None:
                pass

        self.assertTrue(
            process_next_plate_solve_job(
                session_factory=self.Session,
                client_factory=ResumeJobClient,
                worker_id="retry-worker",
                api_key="test-key",
                provider_poll_interval=0,
                provider_timeout=1,
            )
        )
        self.db.refresh(failed)
        self.assertEqual(failed.status, PlateSolveJobStatus.COMPLETED)
        self.assertEqual(failed.provider_submission_id, 15936182)
        self.assertEqual(failed.provider_job_id, 16771675)
        self.assertEqual(failed.attempts, 1)

    def test_failed_retry_recovers_provider_job_from_saved_submission(self) -> None:
        common_file = self._file()
        self._record(common_file)
        queue = PlateSolveQueueService(self.db)
        claimed = queue.claim_next(worker_id="setup-worker", lease_seconds=60)
        queue.record_submission(
            job_id=claimed.id,
            worker_id="setup-worker",
            submission_id=15936182,
        )
        failed = queue.fail(
            job_id=claimed.id,
            worker_id="setup-worker",
            error_message="status lookup failed",
        )
        PlateSolveService(self.db).retry(job_id=failed.id)
        test_case = self
        failed_job_id = failed.id

        class RecoverJobClient:
            def __init__(self, **_kwargs) -> None:
                pass

            def submit(self, *, image_path: str):
                raise AssertionError("saved submission must not be uploaded again")

            def get_submission_status(self, *, submission_id: int):
                self.submission_id = submission_id
                return {
                    "status": "PROCESSING",
                    "submission_id": submission_id,
                    "provider_job_id": 16771675,
                }

            def get_job_status(self, *, submission_id: int, provider_job_id: int):
                verification_db = test_case.Session()
                try:
                    persisted = verification_db.get(AstroPlateSolveJob, failed_job_id)
                    test_case.assertEqual(
                        persisted.provider_job_id,
                        16771675,
                        "provider_job_id must be committed before job polling",
                    )
                finally:
                    verification_db.close()
                return {
                    "status": "COMPLETED",
                    "submission_id": submission_id,
                    "provider_job_id": provider_job_id,
                    "ra": 10,
                    "dec": 20,
                    "rotation": 30,
                    "pixel_scale": 2,
                    "parity": 1,
                }

            def close(self) -> None:
                pass

        self.assertTrue(
            process_next_plate_solve_job(
                session_factory=self.Session,
                client_factory=RecoverJobClient,
                worker_id="retry-worker",
                api_key="test-key",
                provider_poll_interval=0,
                provider_timeout=1,
            )
        )
        self.db.refresh(failed)
        self.assertEqual(failed.status, PlateSolveJobStatus.COMPLETED)
        self.assertEqual(failed.provider_submission_id, 15936182)
        self.assertEqual(failed.provider_job_id, 16771675)
        self.assertEqual(failed.attempts, 1)

    def test_explicitly_missing_provider_job_allows_one_replacement_submit(self) -> None:
        common_file = self._file()
        self._record(common_file)
        queue = PlateSolveQueueService(self.db)
        claimed = queue.claim_next(worker_id="setup-worker", lease_seconds=60)
        queue.record_submission(
            job_id=claimed.id,
            worker_id="setup-worker",
            submission_id=15936182,
        )
        queue.record_provider_job(
            job_id=claimed.id,
            worker_id="setup-worker",
            provider_job_id=16771675,
        )
        failed = queue.fail(
            job_id=claimed.id,
            worker_id="setup-worker",
            error_message="provider job disappeared",
        )
        PlateSolveService(self.db).retry(job_id=failed.id)

        class MissingThenReplaceClient:
            submit_count = 0

            def __init__(self, **_kwargs) -> None:
                pass

            def submit(self, *, image_path: str):
                type(self).submit_count += 1
                return {"status": "WAITING", "submission_id": 15936395}

            def get_submission_status(self, *, submission_id: int):
                self.assert_new_submission = submission_id
                return {
                    "status": "PROCESSING",
                    "submission_id": submission_id,
                    "provider_job_id": 16771999,
                }

            def get_job_status(self, *, submission_id: int, provider_job_id: int):
                if provider_job_id == 16771675:
                    raise AstrometryProviderWorkNotFound(
                        resource="job",
                        provider_id=provider_job_id,
                    )
                return {
                    "status": "COMPLETED",
                    "submission_id": submission_id,
                    "provider_job_id": provider_job_id,
                    "ra": 10,
                    "dec": 20,
                    "rotation": 30,
                    "pixel_scale": 2,
                    "parity": 1,
                }

            def close(self) -> None:
                pass

        self.assertTrue(
            process_next_plate_solve_job(
                session_factory=self.Session,
                client_factory=MissingThenReplaceClient,
                worker_id="retry-worker",
                api_key="test-key",
                provider_poll_interval=0,
                provider_timeout=1,
            )
        )
        self.db.refresh(failed)
        self.assertEqual(MissingThenReplaceClient.submit_count, 1)
        self.assertEqual(failed.status, PlateSolveJobStatus.COMPLETED)
        self.assertEqual(failed.provider_submission_id, 15936395)
        self.assertEqual(failed.provider_job_id, 16771999)
        self.assertEqual(failed.attempts, 2)

    def test_worker_provider_failure_marks_job_failed(self) -> None:
        common_file = self._file()
        self._record(common_file)

        class FailedClient:
            def __init__(self, **_kwargs) -> None:
                pass

            def submit(self, *, image_path: str):
                return {"status": "WAITING", "submission_id": 41}

            def get_submission_status(self, *, submission_id: int):
                return {
                    "status": "PROCESSING",
                    "submission_id": submission_id,
                    "provider_job_id": 99,
                }

            def get_job_status(self, *, submission_id: int, provider_job_id: int):
                return {
                    "status": "FAILED",
                    "submission_id": submission_id,
                    "provider_job_id": provider_job_id,
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

    def test_new_submission_waits_for_job_then_persists_and_completes(self) -> None:
        common_file = self._file()
        self._record(common_file)
        queued = self.db.query(AstroPlateSolveJob).one()
        queued_job_id = queued.id
        test_case = self

        class DelayedJobClient:
            submit_count = 0
            submission_lookups = 0
            job_lookups = 0

            def __init__(self, **_kwargs) -> None:
                pass

            def submit(self, *, image_path: str):
                type(self).submit_count += 1
                return {"status": "WAITING", "submission_id": 15936597}

            def get_submission_status(self, *, submission_id: int):
                test_case.assertEqual(submission_id, 15936597)
                type(self).submission_lookups += 1
                if type(self).submission_lookups == 1:
                    return {
                        "status": "WAITING",
                        "submission_id": submission_id,
                        "provider_job_id": None,
                        "processing_finished": "2026-08-29 00:00:01.000000",
                    }
                return {
                    "status": "PROCESSING",
                    "submission_id": submission_id,
                    "provider_job_id": 16772087,
                    "processing_finished": "2026-08-29 00:00:01.000000",
                }

            def get_job_status(self, *, submission_id: int, provider_job_id: int):
                type(self).job_lookups += 1
                test_case.assertEqual(submission_id, 15936597)
                test_case.assertEqual(provider_job_id, 16772087)
                verification_db = test_case.Session()
                try:
                    persisted = verification_db.get(AstroPlateSolveJob, queued_job_id)
                    test_case.assertEqual(
                        persisted.provider_submission_id,
                        15936597,
                    )
                    test_case.assertEqual(
                        persisted.provider_job_id,
                        16772087,
                        "provider_job_id must be committed before job polling",
                    )
                finally:
                    verification_db.close()
                return {
                    "status": "COMPLETED",
                    "submission_id": submission_id,
                    "provider_job_id": provider_job_id,
                    "ra": 83.822,
                    "dec": -5.391,
                    "rotation": 12.5,
                    "pixel_scale": 2.0,
                    "parity": 1,
                }

            def close(self) -> None:
                pass

        self.assertTrue(
            process_next_plate_solve_job(
                session_factory=self.Session,
                client_factory=DelayedJobClient,
                worker_id="new-submit-worker",
                api_key="test-key",
                provider_poll_interval=0,
                provider_timeout=1,
            )
        )
        self.db.refresh(queued)
        self.assertEqual(DelayedJobClient.submit_count, 1)
        self.assertEqual(DelayedJobClient.submission_lookups, 2)
        self.assertEqual(DelayedJobClient.job_lookups, 1)
        self.assertEqual(queued.status, PlateSolveJobStatus.COMPLETED)
        self.assertEqual(queued.provider_submission_id, 15936597)
        self.assertEqual(queued.provider_job_id, 16772087)
        self.assertEqual(queued.attempts, 1)
        self.assertAlmostEqual(queued.field_width, 2.0)
        self.assertAlmostEqual(queued.field_height, 1.0)

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

            def get_submission_status(self, *, submission_id: int):
                type(self).seen_submission_id = submission_id
                return {
                    "status": "PROCESSING",
                    "submission_id": submission_id,
                    "provider_job_id": 99,
                }

            def get_job_status(self, *, submission_id: int, provider_job_id: int):
                return {
                    "status": "COMPLETED",
                    "submission_id": submission_id,
                    "provider_job_id": provider_job_id,
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
        self.assertEqual(job.attempts, 1)
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

            def get_submission_status(self, *, submission_id: int):
                self._assert_no_transaction()
                return {
                    "status": "PROCESSING",
                    "submission_id": submission_id,
                    "provider_job_id": 99,
                }

            def get_job_status(self, *, submission_id: int, provider_job_id: int):
                self._assert_no_transaction()
                return {
                    "status": "COMPLETED",
                    "submission_id": submission_id,
                    "provider_job_id": provider_job_id,
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

    def test_submission_lookup_connection_error_requeues_with_ids_preserved(self) -> None:
        common_file = self._file()
        record = self._record(common_file)
        job = self.db.query(AstroPlateSolveJob).one()
        job.provider_submission_id = 15936182
        job.attempts = 3
        self.db.commit()

        class TransientSubmissionClient:
            def __init__(self, **_kwargs) -> None:
                pass

            def submit(self, *, image_path: str):
                raise AssertionError("existing submission must not be uploaded again")

            def get_submission_status(self, *, submission_id: int):
                self.submission_id = submission_id
                raise requests.ConnectionError(
                    "remote disconnected",
                    response=None,
                )

            def get_job_status(self, *, submission_id: int, provider_job_id: int):
                raise AssertionError("provider job is not resolved yet")

            def close(self) -> None:
                pass

        self.assertTrue(
            process_next_plate_solve_job(
                session_factory=self.Session,
                client_factory=TransientSubmissionClient,
                worker_id="test-worker",
                api_key="test-key",
                provider_poll_interval=0,
                provider_timeout=1,
            )
        )

        self.db.refresh(job)
        self.db.refresh(record)
        self.assertEqual(job.status, PlateSolveJobStatus.WAITING)
        self.assertEqual(job.provider_submission_id, 15936182)
        self.assertIsNone(job.provider_job_id)
        self.assertEqual(job.attempts, 3)
        self.assertIn("remote disconnected", job.last_error)
        self.assertEqual(record.plate_solve_status, PlateSolveJobStatus.WAITING)

    def test_saved_submission_resolves_job_and_completes_without_resubmit(self) -> None:
        common_file = self._file()
        self._record(common_file)
        job = self.db.query(AstroPlateSolveJob).one()
        job.provider_submission_id = 15936182
        job.attempts = 3
        self.db.commit()

        class ResumeSubmissionClient:
            def __init__(self, **_kwargs) -> None:
                pass

            def submit(self, *, image_path: str):
                raise AssertionError("existing submission must not be uploaded again")

            def get_submission_status(self, *, submission_id: int):
                return {
                    "status": "PROCESSING",
                    "submission_id": submission_id,
                    "provider_job_id": 16771675,
                }

            def get_job_status(self, *, submission_id: int, provider_job_id: int):
                self.provider_job_id = provider_job_id
                return {
                    "status": "COMPLETED",
                    "submission_id": submission_id,
                    "provider_job_id": provider_job_id,
                    "ra": 10,
                    "dec": 20,
                    "rotation": 30,
                    "pixel_scale": 2,
                    "parity": 1,
                }

            def close(self) -> None:
                pass

        self.assertTrue(
            process_next_plate_solve_job(
                session_factory=self.Session,
                client_factory=ResumeSubmissionClient,
                worker_id="test-worker",
                api_key="test-key",
                provider_poll_interval=0,
                provider_timeout=1,
            )
        )

        self.db.refresh(job)
        self.assertEqual(job.status, PlateSolveJobStatus.COMPLETED)
        self.assertEqual(job.provider_submission_id, 15936182)
        self.assertEqual(job.provider_job_id, 16771675)
        self.assertEqual(job.attempts, 3)
        self.assertEqual(job.field_width, 2.0)
        self.assertEqual(job.field_height, 1.0)

    def test_saved_provider_job_transient_error_resumes_without_submission_lookup(self) -> None:
        common_file = self._file()
        self._record(common_file)
        job = self.db.query(AstroPlateSolveJob).one()
        job.provider_submission_id = 15936182
        job.provider_job_id = 16771675
        job.attempts = 3
        job.status = PlateSolveJobStatus.FAILED
        job.completed_at = datetime.now(timezone.utc)
        self.db.commit()
        PlateSolveService(self.db).retry(job_id=job.id)

        class TransientJobClient:
            def __init__(self, **_kwargs) -> None:
                pass

            def submit(self, *, image_path: str):
                raise AssertionError("existing submission must not be uploaded again")

            def get_submission_status(self, *, submission_id: int):
                raise AssertionError("resolved provider job must be reused")

            def get_job_status(self, *, submission_id: int, provider_job_id: int):
                raise requests.ConnectionError("job status unavailable")

            def close(self) -> None:
                pass

        self.assertTrue(
            process_next_plate_solve_job(
                session_factory=self.Session,
                client_factory=TransientJobClient,
                worker_id="worker-a",
                api_key="test-key",
                provider_poll_interval=0,
                provider_timeout=1,
            )
        )
        self.db.refresh(job)
        self.assertEqual(job.status, PlateSolveJobStatus.WAITING)
        self.assertEqual(job.provider_submission_id, 15936182)
        self.assertEqual(job.provider_job_id, 16771675)
        self.assertEqual(job.attempts, 3)

        class RestartedWorkerClient(TransientJobClient):
            def get_job_status(self, *, submission_id: int, provider_job_id: int):
                return {
                    "status": "COMPLETED",
                    "submission_id": submission_id,
                    "provider_job_id": provider_job_id,
                    "ra": 10,
                    "dec": 20,
                    "rotation": 30,
                    "pixel_scale": 2,
                    "parity": 1,
                }

        self.assertTrue(
            process_next_plate_solve_job(
                session_factory=self.Session,
                client_factory=RestartedWorkerClient,
                worker_id="worker-b",
                api_key="test-key",
                provider_poll_interval=0,
                provider_timeout=1,
            )
        )
        self.db.refresh(job)
        self.assertEqual(job.status, PlateSolveJobStatus.COMPLETED)
        self.assertEqual(job.provider_submission_id, 15936182)
        self.assertEqual(job.provider_job_id, 16771675)
        self.assertEqual(job.attempts, 3)

    def test_transient_provider_error_classification(self) -> None:
        transient = [
            requests.Timeout("timeout"),
            requests.ConnectionError("connection"),
            RemoteDisconnected("remote disconnected"),
            ApiClientError("http 408", status_code=408),
            ApiClientError("http 429", status_code=429),
            ApiClientError("http 500", status_code=500),
            ApiClientError("http 503", status_code=503),
        ]
        for error in transient:
            with self.subTest(error=error):
                self.assertTrue(_is_transient_provider_error(error))

        self.assertFalse(
            _is_transient_provider_error(ApiClientError("http 400", status_code=400))
        )


if __name__ == "__main__":
    unittest.main()
