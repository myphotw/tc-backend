from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.config import settings
from app.common.database import Base
from app.common.models.api_usage import CommonApiUsage
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.vision_job import CommonVisionJob
from app.common.repositories.api_usage_repository import (
    ApiName,
    ApiProvider,
    ApiUsageLimitExceeded,
    ApiUsageRepository,
)
from app.common.repositories.vision_job_repository import VisionJobStatus
from app.common.services.api_clients.google.vision_client import VisionClient
from app.memorykeeper.services.auto_tag_service import MemoryKeeperAutoTagService
from worker.vision_worker import process_next_vision_job


class _FakeVisionClient:
    def __init__(self) -> None:
        self.calls = 0

    def label_detection(self, *, image):
        self.calls += 1
        return SimpleNamespace(
            error=SimpleNamespace(message=""),
            label_annotations=[],
        )


class VisionSafeLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        self.temp = tempfile.TemporaryDirectory()
        self.image = Path(self.temp.name) / "image.jpg"
        self.image.write_bytes(b"not-a-real-image-required-by-fake-client")
        self.counter = 0

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def usage(self, used: int, *, year: int | None = None, month: int | None = None):
        now = datetime.now(timezone.utc)
        item = CommonApiUsage(
            provider=ApiProvider.GOOGLE,
            api_name=ApiName.VISION,
            year=year or now.year,
            month=month or now.month,
            used_unit=used,
            limit_unit=900,
            remaining_unit=max(0, 900 - used),
            deleted=False,
        )
        self.db.add(item)
        self.db.commit()
        return item

    def file_and_job(self, *, status: str, retry_count: int = 0):
        self.counter += 1
        digest = f"{self.counter:064x}"
        common_file = CommonFile(
            file_id=digest,
            original_name=f"{digest}.jpg",
            service_name="MemoryKeeper",
            deleted=False,
        )
        self.db.add(common_file)
        self.db.flush()
        self.db.add(
            CommonFileService(file_id=common_file.id, service_name="MemoryKeeper")
        )
        job = CommonVisionJob(
            file_id=common_file.id,
            priority=10,
            status=status,
            retry_count=retry_count,
            vision_provider="GOOGLE",
            requested_at=datetime.now(timezone.utc),
            last_error="temporary" if status == VisionJobStatus.FAILED else None,
            deleted=False,
        )
        self.db.add(job)
        self.db.commit()
        return common_file, job

    def analyze(self, fake: _FakeVisionClient | None = None):
        provider = fake or _FakeVisionClient()
        result = VisionClient(db=self.db, client=provider).analyze(
            image_path=str(self.image)
        )
        return provider, result

    def test_usage_zero_and_899_reserve_before_provider_call(self) -> None:
        first_provider, _result = self.analyze()
        row = self.db.query(CommonApiUsage).one()
        self.assertEqual(first_provider.calls, 1)
        self.assertEqual(row.used_unit, 1)
        self.db.delete(row)
        self.db.commit()
        self.usage(899)

        last_provider, _result = self.analyze()

        self.assertEqual(last_provider.calls, 1)
        row = self.db.query(CommonApiUsage).filter_by(deleted=False).one()
        self.assertEqual(row.used_unit, 900)
        self.assertEqual(row.remaining_unit, 0)

    def test_usage_900_blocks_provider_and_worker_keeps_waiting(self) -> None:
        self.usage(900)
        provider = _FakeVisionClient()
        with self.assertRaises(ApiUsageLimitExceeded):
            self.analyze(provider)
        self.assertEqual(provider.calls, 0)

        _file, job = self.file_and_job(status=VisionJobStatus.WAITING)
        with (
            patch("worker.vision_worker.process_vision_job") as process,
            patch("worker.vision_worker._wait_with_heartbeat") as wait,
        ):
            processed = process_next_vision_job(self.db)

        self.assertTrue(processed)
        process.assert_not_called()
        wait.assert_called_once()
        self.db.refresh(job)
        self.assertEqual(job.status, VisionJobStatus.WAITING)
        self.assertEqual(job.retry_count, 0)
        self.assertIsNone(job.last_error)

    def test_atomic_denial_after_advisory_check_returns_processing_job_to_waiting(self) -> None:
        self.usage(899)
        _file, job = self.file_and_job(status=VisionJobStatus.WAITING)
        with (
            patch(
                "worker.vision_worker.ApiUsageRepository.can_use",
                return_value=True,
            ),
            patch(
                "worker.vision_worker.process_vision_job",
                side_effect=ApiUsageLimitExceeded("race lost"),
            ),
            patch("worker.vision_worker._wait_with_heartbeat"),
        ):
            process_next_vision_job(self.db)

        self.db.refresh(job)
        self.assertEqual(job.status, VisionJobStatus.WAITING)
        self.assertEqual(job.retry_count, 0)
        self.assertIsNone(job.last_error)

    def test_failed_retry_cannot_bypass_exhausted_cap(self) -> None:
        self.usage(900)
        _file, job = self.file_and_job(
            status=VisionJobStatus.FAILED,
            retry_count=1,
        )
        requeued = MemoryKeeperAutoTagService(self.db).retry_job(job.id)
        self.assertEqual(requeued.requeued_count, 1)
        with (
            patch("worker.vision_worker.process_vision_job") as process,
            patch("worker.vision_worker._wait_with_heartbeat"),
        ):
            process_next_vision_job(self.db)

        process.assert_not_called()
        self.db.refresh(job)
        self.assertEqual(job.status, VisionJobStatus.WAITING)
        self.assertEqual(job.retry_count, 1)

    def test_previous_month_exhaustion_does_not_block_current_month_waiting_job(self) -> None:
        now = datetime.now(timezone.utc)
        previous_year = now.year if now.month > 1 else now.year - 1
        previous_month = now.month - 1 if now.month > 1 else 12
        self.usage(900, year=previous_year, month=previous_month)
        _file, job = self.file_and_job(status=VisionJobStatus.WAITING)
        provider = _FakeVisionClient()

        def process(db, _job):
            VisionClient(db=db, client=provider).analyze(image_path=str(self.image))

        with patch("worker.vision_worker.process_vision_job", side_effect=process):
            processed = process_next_vision_job(self.db)

        self.assertTrue(processed)
        self.assertEqual(provider.calls, 1)
        self.db.refresh(job)
        self.assertEqual(job.status, VisionJobStatus.COMPLETED)
        current = (
            self.db.query(CommonApiUsage)
            .filter_by(year=now.year, month=now.month)
            .one()
        )
        self.assertEqual(current.used_unit, 1)

    def test_effective_limit_never_exceeds_900_and_respects_lower_config(self) -> None:
        with patch.object(settings, "VISION_MONTHLY_LIMIT", 1000):
            self.assertEqual(ApiUsageRepository.effective_limit(ApiName.VISION), 900)
        with patch.object(settings, "VISION_MONTHLY_LIMIT", 2000):
            self.assertEqual(ApiUsageRepository.effective_limit(ApiName.VISION), 900)
        with patch.object(settings, "VISION_MONTHLY_LIMIT", 500):
            self.assertEqual(ApiUsageRepository.effective_limit(ApiName.VISION), 500)

    def test_project_wide_counter_blocks_next_service_after_900(self) -> None:
        self.usage(899)
        memory_provider, _result = self.analyze()
        astro_provider = _FakeVisionClient()
        with self.assertRaises(ApiUsageLimitExceeded):
            self.analyze(astro_provider)

        self.assertEqual(memory_provider.calls, 1)
        self.assertEqual(astro_provider.calls, 0)
        self.assertEqual(self.db.query(CommonApiUsage).one().used_unit, 900)

    def test_concurrent_reservations_cannot_cross_900(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "usage.sqlite"
            engine = create_engine(
                f"sqlite:///{database.as_posix()}",
                connect_args={"check_same_thread": False, "timeout": 10},
            )
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine, expire_on_commit=False)
            seed = Session()
            now = datetime.now(timezone.utc)
            seed.add(
                CommonApiUsage(
                    provider=ApiProvider.GOOGLE,
                    api_name=ApiName.VISION,
                    year=now.year,
                    month=now.month,
                    used_unit=899,
                    limit_unit=900,
                    remaining_unit=1,
                    deleted=False,
                )
            )
            seed.commit()
            seed.close()
            barrier = threading.Barrier(2)

            def reserve() -> bool:
                session = Session()
                try:
                    barrier.wait()
                    return ApiUsageRepository(session).reserve_usage(
                        provider=ApiProvider.GOOGLE,
                        api_name=ApiName.VISION,
                    )
                finally:
                    session.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _index: reserve(), range(2)))

            verify = Session()
            try:
                usage = verify.query(CommonApiUsage).one()
                self.assertEqual(sorted(results), [False, True])
                self.assertEqual(usage.used_unit, 900)
                self.assertEqual(usage.remaining_unit, 0)
            finally:
                verify.close()
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
