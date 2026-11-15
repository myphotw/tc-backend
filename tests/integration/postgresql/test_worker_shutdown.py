"""Shutdown transaction checks using only the guarded disposable PostgreSQL DB."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.common.model_registry import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.upload_job import UploadJob
from app.common.models.vision_job import CommonVisionJob
from app.common.repositories.change_event_repository import ChangeEventRepository
from app.common.repositories.upload_job_repository import UploadJobRepository
from app.common.services.changes_service import ChangesService
from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.models.plate_solve_job import AstroPlateSolveJob
from app.astrojournal.services.plate_solve_queue_service import PlateSolveQueueService
from worker import background_worker, vision_worker, plate_solve_worker
from worker.shutdown import WorkerShutdown


pytestmark = pytest.mark.postgresql_integration


def test_upload_shutdown_commits_claimed_job_and_releases_sessions(postgresql_engine):
    Base.metadata.create_all(postgresql_engine)
    factory = sessionmaker(postgresql_engine, expire_on_commit=False)
    stopping = WorkerShutdown()
    with factory() as db:
        db.add(UploadJob(job_id="pg-shutdown-upload", source_type="UPLOAD",
                         status="WAITING", incoming_path="incoming/test.jpg"))
        db.commit()

    def process(db, job, **kwargs):
        stopping.request()
        UploadJobRepository(db).mark_completed(job, file_id="a" * 64)

    with patch.object(background_worker, "SessionLocal", factory), patch.object(
        background_worker, "process_upload_job", side_effect=process
    ) as pipeline:
        assert background_worker.process_next_job(shutdown=stopping)
        assert not background_worker.process_next_job(shutdown=stopping)
        pipeline.assert_called_once()
    with factory() as db:
        assert db.query(UploadJob).one().status == "COMPLETED"


def test_vision_shutdown_commits_event_and_completion_before_exit(postgresql_engine):
    Base.metadata.create_all(postgresql_engine)
    stopping = WorkerShutdown()
    with Session(postgresql_engine) as db:
        file = CommonFile(file_id="b" * 64, original_name="test.jpg", deleted=False)
        db.add(file)
        db.flush()
        db.add(CommonVisionJob(file_id=file.id, status="WAITING", priority=10,
                               vision_provider="GOOGLE", deleted=False, retry_count=0))
        db.commit()

    def process(db, job):
        # Stop while an actual fenced event is still uncommitted.
        ChangeEventRepository(db).append(
            service_name="MemoryKeeper", resource_type="MemoryKeeperFileTag",
            resource_id="b" * 64, operation="UPDATE", revision=1,
        )
        stopping.request()

    with Session(postgresql_engine) as db, patch.object(
        vision_worker.ApiUsageRepository, "can_use", return_value=True
    ), patch.object(vision_worker, "process_vision_job", side_effect=process) as pipeline:
        assert vision_worker.process_next_vision_job(db, shutdown=stopping)
        assert not vision_worker.process_next_vision_job(db, shutdown=stopping)
        pipeline.assert_called_once()
    with Session(postgresql_engine) as db:
        assert db.query(CommonVisionJob).one().status == "COMPLETED"
        assert len(ChangesService(db).list_changes().items) == 1


def test_plate_shutdown_returns_lease_preserves_ids_and_new_worker_resumes(
    postgresql_engine, tmp_path
):
    Base.metadata.create_all(postgresql_engine)
    factory = sessionmaker(postgresql_engine, expire_on_commit=False)
    image = tmp_path / "plate.fits"
    image.write_bytes(b"local-only fixture")
    with factory() as db:
        file = CommonFile(file_id="c" * 64, original_name="plate.fits",
                          original_path=str(image), deleted=False)
        db.add(file)
        db.flush()
        db.add(CommonFileService(file_id=file.id, service_name="AstroJournal"))
        record = ObservationRecord(file_id=file.id, captured_at=datetime.now(timezone.utc))
        db.add(record)
        db.commit()
        queue = PlateSolveQueueService(db)
        job, _ = queue.enqueue(common_file_id=file.id, observation_record_id=record.id)
        job.provider_submission_id, job.provider_job_id = 15936597, 16772087
        job.attempts = 1
        db.commit()
        job_id = job.id
    stopping = WorkerShutdown()
    client = MagicMock()
    client.get_job_status.side_effect = lambda **_: (stopping.request() or {"status": "PROCESSING"})
    client.submit.side_effect = AssertionError("saved provider work must not be uploaded")
    assert plate_solve_worker.process_next_plate_solve_job(
        session_factory=factory, client_factory=lambda **_: client, api_key="test",
        worker_id="old-owner", shutdown=stopping, provider_poll_interval=0,
    )
    with factory() as db:
        job = db.get(AstroPlateSolveJob, job_id)
        assert job.status == "WAITING"
        assert job.worker_id is None and job.lease_expires_at is None
        assert job.provider_submission_id == 15936597
        assert job.provider_job_id == 16772087
        assert job.attempts == 1
        assert db.get(ObservationRecord, record.id).plate_solve_status == "WAITING"
    client.get_job_status.side_effect = None
    client.get_job_status.return_value = {"status": "COMPLETED", "provider_job_id": 16772087}
    with patch.object(plate_solve_worker, "_fetch_optional_wcs", return_value=None):
        assert plate_solve_worker.process_next_plate_solve_job(
            session_factory=factory, client_factory=lambda **_: client, api_key="test",
            worker_id="new-owner", provider_poll_interval=0,
        )
    with factory() as db:
        job = db.get(AstroPlateSolveJob, job_id)
        assert job.status == "COMPLETED" and job.attempts == 1
        assert db.query(CommonChangeEvent).count() >= 4
    client.submit.assert_not_called()
