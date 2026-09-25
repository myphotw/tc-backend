from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from scripts.db_migrate import run_stamp_baseline, run_upgrade
from scripts.plan_memorykeeper_source_year_reconciliation import (
    EXIF_YEAR_CONFLICT,
    SET_YEAR_ONLY,
)
from scripts.reconcile_memorykeeper_source_years import (
    ALREADY_APPLIED,
    READY_YEAR_ONLY_EXIF_CONFLICT,
    MemoryKeeperSourceYearReconciler,
    ReconciliationPlanRow,
)
from tests.integration.postgresql.support import (
    create_legacy_schema,
    run_with_engine_patch,
)


pytestmark = pytest.mark.postgresql_integration


def test_reconciliation_uses_postgresql_generated_columns_and_is_idempotent(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    create_legacy_schema(postgresql_engine)
    run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    run_with_engine_patch(migration_engine_factory, lambda: run_upgrade("head"))

    with Session(postgresql_engine, expire_on_commit=False) as session:
        common_file = CommonFile(
            file_id="a" * 64,
            original_name="source-year.jpg",
            service_name="MemoryKeeper",
            deleted=False,
        )
        session.add(common_file)
        session.flush()
        session.add_all(
            [
                CommonFileService(
                    file_id=common_file.id,
                    service_name="MemoryKeeper",
                ),
                CommonFileMetadata(
                    file_id=common_file.id,
                    original_capture_datetime=datetime(2013, 6, 15, 12, 30),
                ),
                MemoryKeeperFileState(
                    file_id=common_file.id,
                    favorite=False,
                    revision=3,
                    effective_capture_datetime=datetime(2013, 6, 15, 12, 30),
                    effective_capture_year=2013,
                    effective_capture_precision="DATETIME",
                    date_basis="EXIF",
                ),
            ]
        )
        session.commit()
        state = session.get(MemoryKeeperFileState, common_file.id)
        assert state is not None
        plan = ReconciliationPlanRow(
            row_number=2,
            sha256=common_file.file_id,
            source_year=2020,
            action=SET_YEAR_ONLY,
            reason=EXIF_YEAR_CONFLICT,
            review_reason=None,
            current_date_basis=state.date_basis,
            current_effective_year=state.effective_capture_year,
            current_effective_date=state.effective_capture_date,
            current_effective_datetime=state.effective_capture_datetime,
            current_user_datetime=state.user_capture_datetime,
            current_user_precision=state.user_capture_precision,
            current_revision=state.revision,
            source_path_count=1,
        )
        reconciler = MemoryKeeperSourceYearReconciler(session)
        verified = reconciler.verify_all([plan])
        assert verified[0].verification_status == READY_YEAR_ONLY_EXIF_CONFLICT

        stats = reconciler.apply_verified(verified)
        assert stats.applied == 1
        stored = session.execute(
            text(
                """
                SELECT source_capture_year,
                       source_capture_year_basis,
                       effective_capture_datetime,
                       effective_capture_date,
                       effective_capture_year,
                       effective_capture_year_v2,
                       effective_capture_precision,
                       date_basis,
                       revision
                FROM memorykeeper_file_states
                WHERE file_id = :file_id
                """
            ),
            {"file_id": common_file.id},
        ).one()
        assert stored == (
            2020,
            "ORIGINAL_PATH",
            None,
            None,
            None,
            2020,
            "YEAR",
            "SOURCE_YEAR",
            4,
        )

        rerun = reconciler.verify_all([plan])
        assert rerun[0].verification_status == ALREADY_APPLIED
