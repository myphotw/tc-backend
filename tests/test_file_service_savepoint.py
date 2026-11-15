from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.repositories.file_service_repository import FileServiceRepository


@pytest.mark.parametrize("commit", [False, True])
def test_duplicate_savepoint_preserves_caller_changes(commit):
    engine = create_engine("sqlite:///:memory:")
    CommonFile.__table__.create(engine)
    CommonFileService.__table__.create(engine)
    try:
        with Session(engine) as db:
            file = CommonFile(file_id="a" * 64, original_name="before", deleted=True)
            db.add(file)
            db.flush()
            db.add(CommonFileService(file_id=file.id, service_name="AstroJournal"))
            db.commit()
            file_id = file.id
            file.deleted = False
            file.original_name = "restored"
            db.flush()
            repo = FileServiceRepository(db)
            original_get = repo.get
            calls = 0

            def stale_then_current(**kwargs):
                nonlocal calls
                calls += 1
                return None if calls == 1 else original_get(**kwargs)

            # SQLite cannot reproduce a PostgreSQL concurrent uniqueness race.
            # Only force the stale pre-check; the UNIQUE failure is real SQL.
            with patch.object(repo, "get", side_effect=stale_then_current):
                link, created = repo.ensure_link(
                    file_id=file_id, service_name="AstroJournal", commit=commit)
            assert not created
            assert link.file_id == file_id
            assert db.is_active
            db.commit()
        with Session(engine) as verify:
            restored = verify.get(CommonFile, file_id)
            assert restored.deleted is False
            assert restored.original_name == "restored"
            assert verify.query(CommonFileService).count() == 1
    finally:
        engine.dispose()


def test_integrity_failure_without_existing_link_propagates_and_keeps_outer_active():
    engine = create_engine("sqlite:///:memory:")
    CommonFile.__table__.create(engine)
    CommonFileService.__table__.create(engine)
    try:
        with Session(engine) as db:
            file = CommonFile(file_id="b" * 64, original_name="before", deleted=True)
            db.add(file)
            db.commit()
            file.deleted = False
            db.flush()
            # A real NOT NULL violation is not a successful duplicate.
            with pytest.raises(IntegrityError):
                FileServiceRepository(db).ensure_link(
                    file_id=file.id, service_name=None, commit=False)
            assert db.is_active
            assert file.deleted is False
            assert db.query(CommonFileService).count() == 0
            db.rollback()
    finally:
        engine.dispose()


def test_unique_conflict_without_requery_result_is_not_silently_accepted():
    engine = create_engine("sqlite:///:memory:")
    CommonFile.__table__.create(engine)
    CommonFileService.__table__.create(engine)
    try:
        with Session(engine) as db:
            file = CommonFile(file_id="c" * 64, original_name="before", deleted=True)
            db.add(file)
            db.flush()
            db.add(CommonFileService(file_id=file.id, service_name="AstroJournal"))
            db.commit()
            file.deleted = False
            db.flush()
            repo = FileServiceRepository(db)
            # Real UNIQUE error, but no winner can be confirmed afterwards
            # (e.g. it was unlinked concurrently). Never claim success.
            with patch.object(repo, "get", return_value=None):
                with pytest.raises(IntegrityError):
                    repo.ensure_link(file_id=file.id, service_name="AstroJournal", commit=False)
            assert db.is_active
            assert file.deleted is False
            db.rollback()
    finally:
        engine.dispose()
