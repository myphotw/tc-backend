"""Local-only shutdown tests: real signals, SQLite state and mocked providers."""

from datetime import datetime, timezone
import importlib
from pathlib import Path
from types import SimpleNamespace
import signal
import subprocess
import sys
from threading import Thread
import time
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.common.model_registry import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.upload_job import UploadJob
from app.common.models.vision_job import CommonVisionJob
from app.common.repositories.upload_job_repository import UploadJobRepository
from app.common.repositories.api_usage_repository import ApiUsageLimitExceeded
from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.models.plate_solve_job import AstroPlateSolveJob
from app.astrojournal.services.plate_solve_queue_service import PlateSolveQueueService
from worker.shutdown import WorkerShutdown, WorkerShutdownRequested
from worker.worker_monitor import WorkerMonitor
from worker import background_worker, vision_worker, plate_solve_worker


WORKERS = [
    ("worker.background_worker", "process_next_job"),
    ("worker.vision_worker", "process_next_vision_job"),
    ("worker.plate_solve_worker", "process_next_plate_solve_job"),
]


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_real_signal_only_requests_shutdown_and_restores_handlers(signum):
    previous = signal.getsignal(signum)
    stopping = WorkerShutdown()
    with stopping:
        signal.raise_signal(signum)
        assert stopping.requested
        assert stopping.wait(60)
        with pytest.raises(WorkerShutdownRequested):
            stopping.check()
    assert signal.getsignal(signum) == previous


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_real_signal_during_idle_wait_is_not_deferred_until_poll_timeout(signum):
    with WorkerShutdown() as stopping:
        sender = Thread(target=lambda: (time.sleep(0.02), signal.raise_signal(signum)))
        sender.start()
        started = time.monotonic()
        try:
            assert stopping.wait(2)
            assert time.monotonic() - started < 1
        finally:
            sender.join(timeout=1)


@pytest.mark.parametrize("module_name,process_name", WORKERS)
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_worker_finishes_current_call_then_stops_without_another_claim(
    module_name, process_name, signum, monkeypatch
):
    module = importlib.import_module(module_name)
    stopping, monitor = WorkerShutdown(), MagicMock()
    monkeypatch.setattr(module, "initialize_database", lambda: None)
    monkeypatch.setattr(module, "WorkerMonitor", lambda *_: monitor)
    if hasattr(module, "_recover_stale"):
        monkeypatch.setattr(module, "_recover_stale", lambda **_: 0)
    session = MagicMock()
    if module is vision_worker:
        monkeypatch.setattr(module, "SessionLocal", lambda: session)
    completed = []

    def current(*args, **kwargs):
        signal.raise_signal(signum)
        completed.append("commit finished")
        return True

    process = MagicMock(side_effect=current)
    monkeypatch.setattr(module, process_name, process)
    module.run_worker(shutdown=stopping)
    assert completed == ["commit finished"]
    process.assert_called_once()
    monitor.stop.assert_called_once()
    if module is vision_worker:
        session.close.assert_called_once()


@pytest.mark.parametrize("module_name,process_name", WORKERS)
def test_idle_worker_wait_is_interruptible(module_name, process_name, monkeypatch):
    module = importlib.import_module(module_name)
    stopping, monitor = WorkerShutdown(), MagicMock()
    monkeypatch.setattr(module, "initialize_database", lambda: None)
    monkeypatch.setattr(module, "WorkerMonitor", lambda *_: monitor)
    if hasattr(module, "_recover_stale"):
        monkeypatch.setattr(module, "_recover_stale", lambda **_: 0)
    if module is vision_worker:
        monkeypatch.setattr(module, "SessionLocal", MagicMock())
    requester = []

    def empty(*args, **kwargs):
        thread = Thread(target=lambda: (time.sleep(0.02), stopping.request()))
        requester.append(thread)
        thread.start()
        return False

    process = MagicMock(side_effect=empty)
    monkeypatch.setattr(module, process_name, process)
    started = time.monotonic()
    module.run_worker(poll_interval=60, shutdown=stopping)
    for thread in requester:
        thread.join(timeout=1)
    assert time.monotonic() - started < 2
    process.assert_called_once()
    monitor.stop.assert_called_once()


def test_pre_requested_shutdown_never_opens_claim_sessions():
    stopping = WorkerShutdown()
    stopping.request()
    with patch.object(background_worker, "SessionLocal") as upload_session:
        assert not background_worker.process_next_job(shutdown=stopping)
        upload_session.assert_not_called()
    db, factory = MagicMock(), MagicMock()
    assert not vision_worker.process_next_vision_job(db, shutdown=stopping)
    db.query.assert_not_called()
    assert not plate_solve_worker.process_next_plate_solve_job(
        session_factory=factory, shutdown=stopping
    )
    factory.assert_not_called()


def test_shutdown_between_session_creation_or_lookup_and_claim():
    stopping, session = WorkerShutdown(), MagicMock()

    def factory():
        stopping.request()
        return session

    with patch.object(background_worker, "SessionLocal", factory), patch.object(
        background_worker, "UploadJobRepository"
    ) as repository:
        assert not background_worker.process_next_job(shutdown=stopping)
        repository.assert_not_called()
        session.close.assert_called_once()
    stopping, session = WorkerShutdown(), MagicMock()
    with patch.object(plate_solve_worker, "PlateSolveQueueService") as queue:
        assert not plate_solve_worker.process_next_plate_solve_job(
            session_factory=factory, shutdown=stopping
        )
        queue.assert_not_called()
        session.close.assert_called_once()
    stopping, repository = WorkerShutdown(), MagicMock()
    repository.get_next_waiting_job.side_effect = lambda: (stopping.request() or MagicMock())
    with patch.object(vision_worker, "VisionJobRepository", return_value=repository), patch.object(
        vision_worker.ApiUsageRepository, "can_use", return_value=True
    ):
        assert not vision_worker.process_next_vision_job(MagicMock(), shutdown=stopping)
        repository.mark_processing.assert_not_called()


def test_subprocess_signal_handler_exits_normally():
    # raise_signal works on Windows too (os.kill(SIGTERM) there forcibly kills).
    code = (
        "import signal; from worker.shutdown import WorkerShutdown; "
        "s=WorkerShutdown(); "
        "s.__enter__(); signal.raise_signal(signal.SIGTERM); "
        "assert s.requested; s.__exit__(None,None,None); print('STOPPED')"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], capture_output=True, text=True,
        timeout=15, cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "STOPPED"


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


@pytest.mark.parametrize("fail", [False, True])
def test_upload_finishes_commit_or_existing_failure_and_closes_sessions(sessions, fail):
    stopping, monitor = WorkerShutdown(), MagicMock()
    with sessions() as db:
        db.add(UploadJob(job_id="shutdown-upload", source_type="UPLOAD",
                         status="WAITING", incoming_path="incoming/test.jpg"))
        db.commit()
    opened = []

    def factory():
        db = sessions()
        db.close = MagicMock(wraps=db.close)
        opened.append(db)
        return db

    def pipeline(db, job, **kwargs):
        stopping.request()
        if fail:
            raise RuntimeError("pipeline failure")
        UploadJobRepository(db).mark_completed(job, file_id="a" * 64)

    with patch.object(background_worker, "SessionLocal", factory), patch.object(
        background_worker, "process_upload_job", side_effect=pipeline
    ):
        assert background_worker.process_next_job(shutdown=stopping, monitor=monitor)
        assert not background_worker.process_next_job(shutdown=stopping)
    assert len(opened) == 2
    for db in opened:
        db.close.assert_called_once()
    with sessions() as db:
        job = db.query(UploadJob).one()
        assert job.status == ("FAILED" if fail else "COMPLETED")
        assert job.retry_count == (1 if fail else 0)


@pytest.mark.parametrize("stage", ["pipeline", "api", "event", "completion", "quota"])
def test_vision_finishes_transaction_or_returns_waiting_without_extra_work(sessions, stage, tmp_path):
    stopping, monitor = WorkerShutdown(), MagicMock()
    with sessions() as db:
        file = CommonFile(file_id="b" * 64, original_name="vision.jpg", deleted=False)
        db.add(file)
        db.flush()
        db.add(CommonFileService(file_id=file.id, service_name="MemoryKeeper"))
        db.add(CommonVisionJob(file_id=file.id, priority=10, status="WAITING",
                               vision_provider="GOOGLE", deleted=False, retry_count=0))
        db.commit()
    from app.common.repositories.vision_job_repository import VisionJobRepository
    completed = VisionJobRepository.mark_completed
    append = vision_worker._append_memorykeeper_tag_change

    def pipeline(context):
        if stage == "api":
            from app.common.services.api_clients.google.vision_client import VisionClient
            image = tmp_path / "vision.jpg"
            image.write_bytes(b"mock provider does not decode")

            class Provider:
                def label_detection(self, **kwargs):
                    stopping.request()
                    return SimpleNamespace(error=SimpleNamespace(message=""), label_annotations=[])

            VisionClient(db=context.db, client=Provider()).analyze(image_path=str(image))
        if stage in {"pipeline", "quota"}:
            stopping.request()
        if stage == "quota":
            raise ApiUsageLimitExceeded("test cap")

    def event(db, file):
        append(db, file)
        if stage == "event":
            stopping.request()

    def complete(repo, job):
        if stage == "completion":
            stopping.request()
        return completed(repo, job)

    with sessions() as db, patch.object(
        vision_worker.PluginManager, "load_plugins"
    ) as loader, patch.object(
        vision_worker.ApiUsageRepository, "can_use", return_value=True
    ), patch.object(vision_worker, "_append_memorykeeper_tag_change", side_effect=event), patch.object(
        VisionJobRepository, "mark_completed", complete
    ):
        loader.return_value.run.side_effect = pipeline
        assert vision_worker.process_next_vision_job(db, shutdown=stopping, monitor=monitor)
        assert not vision_worker.process_next_vision_job(db, shutdown=stopping)
        loader.return_value.run.assert_called_once()
    with sessions() as db:
        job = db.query(CommonVisionJob).one()
        assert job.status == ("WAITING" if stage == "quota" else "COMPLETED")
        assert job.retry_count == 0
        assert db.query(CommonChangeEvent).count() == (0 if stage == "quota" else 1)


def test_vision_precheck_quota_wait_wakes_without_claiming():
    stopping = WorkerShutdown()
    db, repository = MagicMock(), MagicMock()
    repository.get_next_waiting_job.return_value.id = 1
    with patch.object(vision_worker, "VisionJobRepository", return_value=repository), patch.object(
        vision_worker.ApiUsageRepository, "can_use", side_effect=lambda **_: (stopping.request() or False)
    ):
        assert vision_worker.process_next_vision_job(db, shutdown=stopping)
    repository.mark_processing.assert_not_called()


@pytest.fixture
def plate_job(sessions, tmp_path):
    image = tmp_path / "original.fits"
    image.write_bytes(b"local fixture")
    with sessions() as db:
        file = CommonFile(file_id="c" * 64, original_name="original.fits",
                          original_path=str(image), deleted=False)
        db.add(file)
        db.flush()
        db.add(CommonFileService(file_id=file.id, service_name="AstroJournal"))
        record = ObservationRecord(file_id=file.id, captured_at=datetime.now(timezone.utc))
        db.add(record)
        db.commit()
        job, _ = PlateSolveQueueService(db).enqueue(
            common_file_id=file.id, observation_record_id=record.id
        )
        return job.id


@pytest.mark.parametrize("stage", ["before_submit", "submit", "submission_saved", "job_discovered", "polling", "wcs", "complete"])
def test_plate_shutdown_boundaries_preserve_ids_and_resume_without_submit(
    sessions, plate_job, stage
):
    stopping = WorkerShutdown()
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def submit(self, **kwargs):
            calls.append("submit")
            if stage == "submit":
                stopping.request()  # Request in flight must finish and persist ID.
            return {"submission_id": 15936597}

        def get_submission_status(self, **kwargs):
            if stage == "job_discovered":
                stopping.request()
            return {"status": "PROCESSING", "provider_job_id": 16772087}

        def get_job_status(self, **kwargs):
            if stage == "polling" and not stopping.requested:
                stopping.request()
                return {"status": "PROCESSING"}
            return {"status": "COMPLETED", "provider_job_id": 16772087, "ra": 1}

        def close(self):
            calls.append("close")

    record_submission = plate_solve_worker._record_submission
    complete = PlateSolveQueueService.complete

    def recorded(*args, **kwargs):
        record_submission(*args, **kwargs)
        if stage == "submission_saved":
            stopping.request()

    def wcs(*args, **kwargs):
        if stage == "wcs":
            stopping.request()
        return None

    def completed(service, **kwargs):
        if stage == "complete":
            stopping.request()
        return complete(service, **kwargs)

    reserve = MagicMock(side_effect=lambda *_: stopping.check())
    load = plate_solve_worker._load_work

    def loaded(*args, **kwargs):
        result = load(*args, **kwargs)
        if stage == "before_submit":
            stopping.request()
        return result

    with patch.object(plate_solve_worker, "_reserve_submit_usage", reserve), patch.object(
        plate_solve_worker, "_record_submission", side_effect=recorded
    ), patch.object(plate_solve_worker, "_load_work", side_effect=loaded), patch.object(
        plate_solve_worker, "_fetch_optional_wcs", side_effect=wcs
    ), patch.object(PlateSolveQueueService, "complete", completed):
        assert plate_solve_worker.process_next_plate_solve_job(
            session_factory=sessions, client_factory=Client, api_key="test",
            worker_id="shutdown-owner", provider_poll_interval=0, shutdown=stopping,
        )
    with sessions() as db:
        job = db.get(AstroPlateSolveJob, plate_job)
        finished = stage in {"wcs", "complete"}
        assert job.status == ("COMPLETED" if finished else "WAITING")
        assert job.worker_id is None and job.lease_expires_at is None
        assert job.attempts == 1
        if stage != "before_submit":
            assert job.provider_submission_id == 15936597
        else:
            assert "submit" not in calls
        if stage in {"job_discovered", "polling", "wcs", "complete"}:
            assert job.provider_job_id == 16772087
    if not finished and stage != "before_submit":
        # Restart must poll existing work, not login/upload or consume quota.
        with patch.object(Client, "submit", side_effect=AssertionError("duplicate submit")), patch.object(
            plate_solve_worker, "_reserve_submit_usage", side_effect=AssertionError("extra quota")
        ):
            assert plate_solve_worker.process_next_plate_solve_job(
                session_factory=sessions, client_factory=Client, api_key="test",
                worker_id="new-owner", provider_poll_interval=0,
            )
        with sessions() as db:
            job = db.get(AstroPlateSolveJob, plate_job)
            assert job.status == "COMPLETED"
            assert job.attempts == 1
    assert "close" in calls


def test_plate_stop_during_claim_returns_saved_job_without_api_calls(sessions, plate_job):
    stopping = WorkerShutdown()
    with sessions() as db:
        job = db.get(AstroPlateSolveJob, plate_job)
        job.provider_submission_id, job.provider_job_id, job.attempts = 15936597, 16772087, 1
        db.commit()
    claim = PlateSolveQueueService.claim_next

    def claimed(queue, **kwargs):
        result = claim(queue, **kwargs)
        stopping.request()
        return result

    client = MagicMock()
    with patch.object(PlateSolveQueueService, "claim_next", claimed):
        assert plate_solve_worker.process_next_plate_solve_job(
            session_factory=sessions, client_factory=client, shutdown=stopping
        )
    client.assert_not_called()
    with sessions() as db:
        job = db.get(AstroPlateSolveJob, plate_job)
        assert job.status == "WAITING" and job.attempts == 1
        assert job.provider_job_id == 16772087
        assert job.worker_id is None and job.lease_expires_at is None


def test_plate_shutdown_does_not_release_a_lease_taken_by_another_worker(sessions, plate_job):
    stopping = WorkerShutdown()
    load = plate_solve_worker._load_work

    def lost(*args, **kwargs):
        result = load(*args, **kwargs)
        with sessions() as db:
            db.get(AstroPlateSolveJob, plate_job).worker_id = "new-owner"
            db.commit()
        stopping.request()
        return result

    with patch.object(plate_solve_worker, "_load_work", side_effect=lost):
        with pytest.raises(RuntimeError, match="lease was lost"):
            plate_solve_worker.process_next_plate_solve_job(
                session_factory=sessions, client_factory=MagicMock(),
                api_key="test", shutdown=stopping, worker_id="old-owner",
            )
    with sessions() as db:
        job = db.get(AstroPlateSolveJob, plate_job)
        assert job.status == "PROCESSING" and job.worker_id == "new-owner"
        assert job.lease_expires_at is not None


def test_monitor_stop_is_idempotent_and_clears_current_job(sessions):
    from app.common.models.worker_status import CommonWorkerStatus
    from app.common.repositories.worker_status_repository import WorkerStatusRepository
    with patch("worker.worker_monitor.SessionLocal", sessions):
        monitor = WorkerMonitor("shutdown-monitor")
        monitor.start()
        monitor.maybe_heartbeat(current_job_id="test-job", force=True)
        with patch("worker.worker_monitor.WorkerStatusRepository", wraps=WorkerStatusRepository) as repository:
            monitor.stop()
            monitor.stop()
            repository.assert_called_once()
    with sessions() as db:
        row = db.get(CommonWorkerStatus, "shutdown-monitor")
        assert row.status == "STOPPED"
        assert row.current_job_id is None


def test_monitor_stop_failure_is_logged_and_does_not_mask_exit():
    monitor = WorkerMonitor("shutdown-failure")
    monitor._started = True
    with patch("worker.worker_monitor.SessionLocal", side_effect=RuntimeError("DB unavailable")) as factory:
        monitor.stop()
        monitor.stop()
        factory.assert_called_once()


def test_monitor_close_failure_does_not_mask_exit():
    monitor, db = WorkerMonitor("close-failure"), MagicMock()
    monitor._started = True
    db.close.side_effect = RuntimeError("connection lost")
    with patch("worker.worker_monitor.SessionLocal", return_value=db), patch(
        "worker.worker_monitor.WorkerStatusRepository"
    ):
        monitor.stop()
        monitor.stop()
    db.close.assert_called_once()


def test_compose_shutdown_settings_preserve_profiles_and_commands():
    import yaml
    compose = yaml.safe_load((Path(__file__).resolve().parents[1] / "docker-compose.nas.yml").read_text())
    services = compose["services"]
    expected = {"backend": "90s", "upload-worker": "5m", "vision-worker": "12m", "plate-solve-worker": "5m"}
    for name, grace in expected.items():
        assert services[name]["stop_grace_period"].endswith(f":-{grace}}}")
        assert services[name]["restart"] == "unless-stopped"
    assert services["vision-worker"]["profiles"] == ["vision"]
    assert services["plate-solve-worker"]["profiles"] == ["astro"]
    assert services["upload-worker"]["command"] == ["python", "-m", "worker.background_worker"]
    assert services["vision-worker"]["command"] == ["python", "-m", "worker.vision_worker"]
    assert services["plate-solve-worker"]["command"] == ["python", "-m", "worker.plate_solve_worker"]
    assert services["backend"]["command"][-2:] == ["--timeout-graceful-shutdown", "60"]
