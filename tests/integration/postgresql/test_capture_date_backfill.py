from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import Barrier

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from scripts.backfill_capture_dates import (
    ExifScanResult,
    _apply_snapshot,
    _get_or_create_state,
    _load_snapshot_batch,
    backfill_capture_dates,
    validate_capture_dates,
)
from scripts.db_migrate import run_stamp_baseline, run_upgrade
from tests.integration.postgresql.support import (
    create_legacy_schema,
    run_with_engine_patch,
)


pytestmark = pytest.mark.postgresql_integration


class NoFilesystemStorage:
    """Projection-only tests must not resolve or read original files."""

    @property
    def original_root(self):  # pragma: no cover - must remain unused
        raise AssertionError("filesystem must not be accessed")

    def resolve_storage_path(self, value):  # pragma: no cover - must remain unused
        raise AssertionError("filesystem must not be accessed")


def _upgrade_to_head(postgresql_engine: Engine, migration_engine_factory) -> None:
    create_legacy_schema(postgresql_engine)
    run_with_engine_patch(migration_engine_factory, run_stamp_baseline)
    run_with_engine_patch(migration_engine_factory, lambda: run_upgrade("head"))


def _seed_active_link(session: Session, suffix: str) -> CommonFile:
    common_file = CommonFile(
        file_id=(suffix * 64)[:64],
        original_name=f"{suffix}.jpg",
        deleted=False,
    )
    session.add(common_file)
    session.flush()
    session.add(CommonFileService(file_id=common_file.id, service_name="MemoryKeeper"))
    session.commit()
    return common_file


def test_backfill_projection_creates_state_and_postgresql_generated_values(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    _upgrade_to_head(postgresql_engine, migration_engine_factory)
    with Session(postgresql_engine, expire_on_commit=False) as session:
        common_file = _seed_active_link(session, "a")
        result = backfill_capture_dates(
            session,
            storage_service=NoFilesystemStorage(),
            execute=True,
            projection_only=True,
        )
        assert result.updated == 1
        state = session.get(MemoryKeeperFileState, common_file.id)
        assert state is not None
        assert state.date_basis == "IMPORTED"
        assert state.effective_capture_datetime is not None
        assert state.effective_capture_date == state.effective_capture_datetime.date()
        assert state.effective_capture_year == state.effective_capture_datetime.year
        assert validate_capture_dates(session).is_clean


def test_state_creation_is_idempotent_and_latest_user_wins(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    _upgrade_to_head(postgresql_engine, migration_engine_factory)
    with Session(postgresql_engine, expire_on_commit=False) as session:
        common_file = _seed_active_link(session, "b")
        snapshot = _load_snapshot_batch(
            session,
            after_file_id=0,
            through_file_id=common_file.id,
            limit=1,
        )[0]
        session.rollback()

        # This models online dual-write creating a state after the backfill
        # snapshot but before its short write transaction begins.
        online_state = MemoryKeeperFileState(
            file_id=common_file.id,
            favorite=True,
            revision=3,
            user_capture_datetime=datetime(2025, 1, 2, 3, 4),
            user_capture_precision="DATETIME",
        )
        session.add(online_state)
        session.commit()

        changed, inactive = _apply_snapshot(
            session,
            snapshot=snapshot,
            scan=ExifScanResult(capture_datetime=datetime(2011, 2, 3, 4, 5)),
        )
        session.commit()
        assert changed
        assert not inactive
        state = session.get(MemoryKeeperFileState, common_file.id)
        assert state is not None
        assert state.user_capture_datetime == datetime(2025, 1, 2, 3, 4)
        assert state.user_capture_precision == "DATETIME"
        assert state.effective_capture_datetime == datetime(2025, 1, 2, 3, 4)
        assert state.date_basis == "USER"
        assert state.revision == 3

        # Re-running creation after another actor has inserted the row is a
        # no-op and does not replace favorite/user/revision semantics.
        existing, created = _get_or_create_state(
            session,
            common_file=common_file,
            state=session.get(MemoryKeeperFileState, common_file.id),
        )
        assert not created
        assert existing.file_id == common_file.id
        assert session.query(MemoryKeeperFileState).count() == 1

        derived = session.execute(
            text(
                "SELECT effective_capture_date, effective_capture_year "
                "FROM memorykeeper_file_states WHERE file_id = :file_id"
            ),
            {"file_id": common_file.id},
        ).one()
        assert derived == (date(2025, 1, 2), 2025)
        assert validate_capture_dates(session).is_clean


def test_concurrent_state_insert_recovers_the_unique_race(
    postgresql_engine: Engine,
    migration_engine_factory,
) -> None:
    _upgrade_to_head(postgresql_engine, migration_engine_factory)
    with Session(postgresql_engine, expire_on_commit=False) as session:
        common_file = _seed_active_link(session, "c")
        common_file_id = common_file.id

    barrier = Barrier(2)

    def create_once() -> bool:
        with Session(postgresql_engine) as session:
            current = session.get(CommonFile, common_file_id)
            assert current is not None
            barrier.wait(timeout=5)
            _, created = _get_or_create_state(
                session,
                common_file=current,
                state=None,
            )
            session.commit()
            return created

    with ThreadPoolExecutor(max_workers=2) as executor:
        created = list(executor.map(lambda _: create_once(), range(2)))

    assert sum(created) == 1
    with Session(postgresql_engine) as session:
        states = (
            session.query(MemoryKeeperFileState)
            .filter(MemoryKeeperFileState.file_id == common_file_id)
            .all()
        )
        assert len(states) == 1
