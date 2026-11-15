"""Real PostgreSQL races; use the existing disposable DB guards unchanged."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.astrojournal.models.observation_record import ObservationRecord
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.repositories.change_event_repository import ChangeEventRepository
from app.common.repositories.file_service_repository import FileServiceRepository
from app.common.services.changes_service import ChangesService


pytestmark = pytest.mark.postgresql_integration


def append(db, *, service="AstroJournal", resource_id=None):
    return ChangeEventRepository(db).append(
        service_name=service, resource_type="FenceTest",
        resource_id=resource_id or str(uuid4()), operation="UPDATE", revision=None)


def feed(engine, **kwargs):
    with Session(engine) as db:
        return ChangesService(db).list_changes(**kwargs)


def assert_busy(engine, **kwargs):
    with pytest.raises(HTTPException) as error:
        feed(engine, **kwargs)
    assert error.value.status_code == 503
    assert error.value.detail == {"code": "CHANGE_FEED_BUSY"}
    assert error.value.headers == {"Retry-After": "1"}


def test_concurrent_link_collision_keeps_flushed_file_and_observation(postgresql_engine):
    for table in (CommonFile.__table__, CommonFileService.__table__, ObservationRecord.__table__):
        table.create(postgresql_engine)
    with Session(postgresql_engine) as seed:
        file = CommonFile(file_id="c" * 64, original_name="before", deleted=True)
        seed.add(file)
        seed.flush()
        record = ObservationRecord(file_id=file.id, captured_at=datetime.now(timezone.utc))
        seed.add(record)
        seed.commit()
        file_id, record_id = file.id, record.id

    for commit in (False, True):
        with Session(postgresql_engine) as clear:
            clear.query(CommonFileService).delete()
            clear.get(CommonFile, file_id).deleted = True
            clear.commit()
        attempted = Event()
        rolled_back_savepoints = []
        with Session(postgresql_engine) as winner:
            winner.add(CommonFileService(file_id=file_id, service_name="AstroJournal"))
            winner.flush()  # Invisible to the contender; holds unique index entry.

            def contend():
                with Session(postgresql_engine) as db:
                    file = db.get(CommonFile, file_id)
                    file.deleted = False
                    db.get(ObservationRecord, record_id).memo = "caller change retained"
                    db.flush()

                    def inserting(conn, cursor, statement, parameters, context, executemany):
                        if statement.startswith("INSERT INTO common_file_services"):
                            attempted.set()
                        if statement.startswith("ROLLBACK TO SAVEPOINT"):
                            rolled_back_savepoints.append(statement)

                    event.listen(db.connection(), "before_cursor_execute", inserting)
                    _, created = FileServiceRepository(db).ensure_link(
                        file_id=file_id, service_name="AstroJournal", commit=commit)
                    assert not created
                    assert db.is_active
                    if not commit:
                        assert db.in_transaction()
                    assert db.get_nested_transaction() is None
                    db.commit()

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(contend)
                try:
                    assert attempted.wait(5)
                    assert not future.done()
                finally:
                    winner.commit()
                future.result(timeout=10)
        assert rolled_back_savepoints
        with Session(postgresql_engine) as verify:
            assert verify.get(CommonFile, file_id).deleted is False
            assert verify.get(ObservationRecord, record_id).memo == "caller change retained"
            assert verify.query(CommonFileService).count() == 1


def test_feed_cannot_pass_lower_uncommitted_id(postgresql_engine):
    CommonChangeEvent.__table__.create(postgresql_engine)
    with Session(postgresql_engine) as first, Session(postgresql_engine) as second:
        low_id = append(first).id
        high_id = append(second).id
        assert low_id < high_id
        second.commit()  # Deliberately reproduce the original commit inversion.
        assert_busy(postgresql_engine)
        first.commit()
    page = feed(postgresql_engine)
    assert [item.cursor for item in page.items] == [low_id, high_id]
    assert page.next_cursor == high_id


def test_missing_link_after_integrity_error_is_explicit_failure(postgresql_engine):
    CommonFile.__table__.create(postgresql_engine)
    CommonFileService.__table__.create(postgresql_engine)
    with Session(postgresql_engine) as db:
        file = CommonFile(file_id="e" * 64, original_name="before", deleted=True)
        db.add(file)
        db.commit()
        file_id = file.id
        file.deleted = False
        db.flush()
        with pytest.raises(IntegrityError):
            FileServiceRepository(db).ensure_link(
                file_id=file_id, service_name=None, commit=False)
        assert db.is_active
        assert db.get(CommonFile, file_id).deleted is False
        db.rollback()
    with Session(postgresql_engine) as verify:
        assert verify.get(CommonFile, file_id).deleted is True
        assert verify.query(CommonFileService).count() == 0


def test_rollback_releases_fence_and_sequence_gap_is_safe(postgresql_engine):
    CommonChangeEvent.__table__.create(postgresql_engine)
    with Session(postgresql_engine) as first:
        rolled_back_id = append(first).id
        assert_busy(postgresql_engine)
        first.rollback()
    with Session(postgresql_engine) as second:
        committed_id = append(second).id
        second.commit()
    assert committed_id > rolled_back_id
    page = feed(postgresql_engine)
    assert [item.cursor for item in page.items] == [committed_id]
    assert not page.has_more


def test_multiple_events_filters_and_existing_cursor(postgresql_engine):
    CommonChangeEvent.__table__.create(postgresql_engine)
    with Session(postgresql_engine) as first, Session(postgresql_engine) as second:
        ids = [append(first).id, append(first).id]
        middle_id = append(second, service="MemoryKeeper").id
        second.commit()
        ids.append(append(first).id)
        assert_busy(postgresql_engine, service_name="MemoryKeeper")
        first.commit()
    one = feed(postgresql_engine, cursor=ids[0], limit=1, service_name="AstroJournal")
    assert [item.cursor for item in one.items] == [ids[1]]
    assert one.has_more
    two = feed(postgresql_engine, cursor=one.next_cursor, limit=1, service_name="AstroJournal")
    assert [item.cursor for item in two.items] == [ids[2]]
    assert not two.has_more
    assert feed(postgresql_engine, service_name="MemoryKeeper").next_cursor == middle_id
    empty = feed(postgresql_engine, cursor=two.next_cursor)
    assert empty.items == []
    assert empty.next_cursor == two.next_cursor
    assert not empty.has_more


def test_direct_orm_insert_also_participates(postgresql_engine):
    CommonChangeEvent.__table__.create(postgresql_engine)
    with Session(postgresql_engine) as writer:
        writer.add(CommonChangeEvent(service_name="MemoryKeeper", resource_type="Direct",
                                     resource_id="one", operation="UPDATE"))
        writer.flush()
        assert_busy(postgresql_engine)
        writer.commit()
    assert len(feed(postgresql_engine).items) == 1


@pytest.mark.parametrize("round_index", range(5))
def test_concurrent_publishers_and_reader_never_skip_committed_events(postgresql_engine, round_index):
    CommonChangeEvent.__table__.create(postgresql_engine)
    ready = Barrier(6)

    def publish(index):
        ids = []
        with Session(postgresql_engine) as db:
            ready.wait(timeout=5)
            for _ in range(3):
                ids.append(append(db, service="AstroJournal" if index % 2 else "MemoryKeeper").id)
                Event().wait(0.002 * (6 - index))
            db.commit()
        return ids

    received, cursor = [], 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(publish, index) for index in range(6)]
        for _ in range(500):
            try:
                page = feed(postgresql_engine, cursor=cursor, limit=2)
                received.extend(item.cursor for item in page.items)
                cursor = page.next_cursor
                if all(future.done() for future in futures) and not page.has_more:
                    break
            except HTTPException as error:
                assert error.status_code == 503
            Event().wait(0.002)
        expected = sorted(item for future in futures for item in future.result(timeout=10))
    assert received == expected
    assert len(received) == len(set(received)) == 18


def test_reader_does_not_queue_and_deadlock_domain_writers(postgresql_engine):
    CommonChangeEvent.__table__.create(postgresql_engine)
    CommonFile.__table__.create(postgresql_engine)
    with Session(postgresql_engine) as seed:
        file = CommonFile(file_id="d" * 64, original_name="before")
        seed.add(file)
        seed.commit()
        file_id = file.id
    with Session(postgresql_engine) as row_owner:
        row_owner.execute(text("UPDATE common_files SET original_name='first' WHERE id=:id"), {"id": file_id})
        holding_fence = Event()

        def other_writer():
            with Session(postgresql_engine) as db:
                append(db)
                holding_fence.set()
                db.execute(text("UPDATE common_files SET original_name='second' WHERE id=:id"), {"id": file_id})
                db.commit()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(other_writer)
            try:
                assert holding_fence.wait(5)
                assert_busy(postgresql_engine)  # No exclusive lock waiter is left behind.
                append(row_owner)  # Shared fence must not wait behind a reader.
                row_owner.commit()
            finally:
                row_owner.rollback()
            future.result(timeout=10)
    assert len(feed(postgresql_engine).items) == 2


def test_feed_rejects_lock_upgrade_and_stale_snapshot_isolation(postgresql_engine):
    CommonChangeEvent.__table__.create(postgresql_engine)
    with Session(postgresql_engine) as db:
        append(db)
        with pytest.raises(RuntimeError, match="independent read-only"):
            ChangesService(db).list_changes()
        db.rollback()
        assert feed(postgresql_engine).items == []
    with postgresql_engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with Session(connection) as db:
            with pytest.raises(RuntimeError, match="READ COMMITTED"):
                ChangesService(db).list_changes()


def test_savepoint_rollback_does_not_cache_a_released_write_fence(postgresql_engine):
    CommonChangeEvent.__table__.create(postgresql_engine)
    with Session(postgresql_engine) as db:
        nested = db.begin_nested()
        discarded_id = append(db).id
        nested.rollback()
        assert feed(postgresql_engine).items == []
        next_id = append(db).id
        assert next_id > discarded_id
        assert_busy(postgresql_engine)
        db.commit()
    assert [item.cursor for item in feed(postgresql_engine).items] == [next_id]


def test_successful_reader_fences_new_id_allocation_until_transaction_end(postgresql_engine):
    CommonChangeEvent.__table__.create(postgresql_engine)
    attempted = Event()
    with Session(postgresql_engine) as reader:
        assert ChangesService(reader).list_changes().items == []

        def publish():
            with Session(postgresql_engine) as db:
                def attempting(conn, cursor, statement, parameters, context, executemany):
                    if "pg_advisory_xact_lock_shared" in statement:
                        attempted.set()

                event.listen(db.connection(), "before_cursor_execute", attempting)
                event_id = append(db).id
                db.commit()
                return event_id

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(publish)
            try:
                assert attempted.wait(5)
                assert not future.done()
            finally:
                reader.rollback()
            published_id = future.result(timeout=10)
    assert feed(postgresql_engine).next_cursor == published_id
