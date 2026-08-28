from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.upload_job import UploadJob
from app.common.repositories.upload_job_repository import (
    UploadJobRepository,
    UploadJobStatus,
)


class UploadJobClaimPriorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.repository = UploadJobRepository(self.db)
        self.base_time = datetime(2026, 8, 26, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _job(
        self,
        job_id: str,
        *,
        service_name: str,
        created_at: datetime,
        status: str = UploadJobStatus.WAITING,
    ) -> UploadJob:
        job = UploadJob(
            job_id=job_id,
            source_type="UPLOAD",
            status=status,
            incoming_path=f"incoming/{job_id}.jpg",
            service_name=service_name,
            created_at=created_at,
        )
        self.db.add(job)
        self.db.commit()
        return job

    def test_astrojournal_is_claimed_before_older_memorykeeper_backlog(self) -> None:
        for index in range(3):
            self._job(
                f"memory-{index}",
                service_name="MemoryKeeper",
                created_at=self.base_time + timedelta(minutes=index),
            )
        astro = self._job(
            "astro-interactive",
            service_name="AstroJournal",
            created_at=self.base_time + timedelta(days=1),
        )

        claimed = self.repository.claim_next_waiting_job("worker-priority-test")

        self.assertEqual(claimed.job_id, astro.job_id)
        self.assertEqual(claimed.status, UploadJobStatus.PROCESSING)

    def test_astrojournal_jobs_keep_created_at_fifo(self) -> None:
        self._job(
            "memory-older",
            service_name="MemoryKeeper",
            created_at=self.base_time,
        )
        newer = self._job(
            "astro-newer",
            service_name="AstroJournal",
            created_at=self.base_time + timedelta(days=2),
        )
        older = self._job(
            "astro-older",
            service_name="AstroJournal",
            created_at=self.base_time + timedelta(days=1),
        )

        first = self.repository.claim_next_waiting_job("worker-priority-test")
        second = self.repository.claim_next_waiting_job("worker-priority-test")
        third = self.repository.claim_next_waiting_job("worker-priority-test")

        self.assertEqual(first.job_id, older.job_id)
        self.assertEqual(second.job_id, newer.job_id)
        self.assertEqual(third.job_id, "memory-older")

    def test_memorykeeper_keeps_fifo_when_no_astrojournal_is_waiting(self) -> None:
        newer = self._job(
            "memory-newer",
            service_name="MemoryKeeper",
            created_at=self.base_time + timedelta(minutes=1),
        )
        older = self._job(
            "memory-older",
            service_name="MemoryKeeper",
            created_at=self.base_time,
        )

        first = self.repository.claim_next_waiting_job("worker-priority-test")
        second = self.repository.claim_next_waiting_job("worker-priority-test")

        self.assertEqual(first.job_id, older.job_id)
        self.assertEqual(second.job_id, newer.job_id)

    def test_processing_job_is_not_claimed(self) -> None:
        processing = self._job(
            "astro-processing",
            service_name="AstroJournal",
            created_at=self.base_time,
            status=UploadJobStatus.PROCESSING,
        )
        waiting = self._job(
            "memory-waiting",
            service_name="MemoryKeeper",
            created_at=self.base_time + timedelta(days=1),
        )

        claimed = self.repository.claim_next_waiting_job("worker-priority-test")

        self.assertEqual(claimed.job_id, waiting.job_id)
        self.db.refresh(processing)
        self.assertEqual(processing.status, UploadJobStatus.PROCESSING)

    def test_postgresql_claim_keeps_priority_order_and_skip_locked(self) -> None:
        query = (
            self.db.query(UploadJob)
            .filter(UploadJob.status == UploadJobStatus.WAITING)
            .order_by(*self.repository._waiting_order_by())
            .with_for_update(skip_locked=True)
            .limit(1)
        )

        sql = str(
            query.statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("CASE common_upload_jobs.service_name", sql)
        self.assertLess(
            sql.index("CASE common_upload_jobs.service_name"),
            sql.index("common_upload_jobs.created_at ASC"),
        )


if __name__ == "__main__":
    unittest.main()
