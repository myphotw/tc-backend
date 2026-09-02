from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.vision_job import CommonVisionJob
from app.common.repositories.vision_job_repository import (
    VisionJobRepository,
    VisionJobStatus,
)
from scripts.db_migrate import run_stamp_baseline, run_upgrade
from tests.integration.postgresql.support import create_legacy_schema, run_with_engine_patch


pytestmark = pytest.mark.postgresql_integration


def _upgrade_to_head(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    create_legacy_schema(postgresql_engine)
    run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    run_with_engine_patch(migration_engine_factory, lambda: run_upgrade("head"))


def _add_stale_job(
    session: Session,
    *,
    suffix: int,
    started_at: datetime,
) -> CommonVisionJob:
    common_file = CommonFile(
        file_id=f"{suffix:064x}",
        original_name=f"{suffix}.jpg",
        deleted=False,
    )
    session.add(common_file)
    session.flush()
    job = CommonVisionJob(
        file_id=common_file.id,
        priority=0,
        status=VisionJobStatus.PROCESSING,
        retry_count=0,
        vision_provider="GOOGLE",
        requested_at=started_at,
        started_at=started_at,
        completed_at=None,
        deleted=False,
    )
    session.add(job)
    session.flush()
    return job


def test_vision_stale_recovery_uses_skip_locked_between_workers(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    _upgrade_to_head(postgresql_engine, migration_engine_factory)
    now = datetime.now(timezone.utc)
    with Session(postgresql_engine) as seed:
        locked_job = _add_stale_job(
            seed,
            suffix=1,
            started_at=now - timedelta(hours=8),
        )
        available_job = _add_stale_job(
            seed,
            suffix=2,
            started_at=now - timedelta(hours=7),
        )
        seed.commit()
        locked_id = locked_job.id
        available_id = available_job.id

    with Session(postgresql_engine) as lock_session:
        lock_session.query(CommonVisionJob).filter(
            CommonVisionJob.id == locked_id
        ).with_for_update().one()

        with Session(postgresql_engine) as recovery_session:
            recovered = VisionJobRepository(
                recovery_session
            ).recover_stale_processing_jobs(
                stale_seconds=6 * 60 * 60,
                worker_name_prefix="VisionWorker",
                live_heartbeat_seconds=90,
            )
            assert recovered == 1
            assert recovery_session.get(CommonVisionJob, available_id).status == (
                VisionJobStatus.WAITING
            )
            assert recovery_session.get(CommonVisionJob, locked_id).status == (
                VisionJobStatus.PROCESSING
            )

        lock_session.commit()

    with Session(postgresql_engine) as final_session:
        recovered = VisionJobRepository(
            final_session
        ).recover_stale_processing_jobs(
            stale_seconds=6 * 60 * 60,
            worker_name_prefix="VisionWorker",
            live_heartbeat_seconds=90,
        )
        assert recovered == 1
        assert final_session.get(CommonVisionJob, locked_id).status == (
            VisionJobStatus.WAITING
        )

