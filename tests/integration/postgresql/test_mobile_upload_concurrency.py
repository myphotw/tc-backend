from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.repositories.file_service_repository import FileServiceRepository
from worker.plugins.storage_plugin import StoragePlugin


pytestmark = pytest.mark.postgresql_integration


def test_same_sha_storage_section_is_serialized_by_transaction_lock(
    postgresql_engine: Engine,
) -> None:
    digest = "f" * 64
    acquired = Event()
    attempted = Event()

    with Session(postgresql_engine) as owner:
        StoragePlugin._acquire_file_storage_lock(
            SimpleNamespace(db=owner, file_id=digest)
        )

        def contend() -> None:
            with Session(postgresql_engine) as contender:
                attempted.set()
                StoragePlugin._acquire_file_storage_lock(
                    SimpleNamespace(db=contender, file_id=digest)
                )
                acquired.set()
                contender.rollback()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(contend)
            attempted_before_release = attempted.wait(timeout=2)
            blocked_before_release = not acquired.wait(timeout=0.1)

            owner.rollback()

            assert acquired.wait(timeout=2)
            future.result(timeout=2)
            assert attempted_before_release
            assert blocked_before_release


def test_concurrent_memorykeeper_link_creation_has_one_winner(
    postgresql_engine: Engine,
) -> None:
    Base.metadata.create_all(
        postgresql_engine,
        tables=[CommonFile.__table__, CommonFileService.__table__],
    )
    digest = "e" * 64
    with Session(postgresql_engine) as seed:
        common_file = CommonFile(
            file_id=digest,
            original_name="concurrency-test.jpg",
            original_path="original/concurrency-test.jpg",
            service_name="AstroJournal",
        )
        seed.add(common_file)
        seed.commit()
        common_file_id = common_file.id

    barrier = Barrier(2)

    def attach_memorykeeper() -> bool:
        with Session(postgresql_engine) as session:
            barrier.wait(timeout=2)
            _, created = FileServiceRepository(session).ensure_link(
                file_id=common_file_id,
                service_name="MemoryKeeper",
            )
            return created

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attach_memorykeeper(), range(2)))

    assert sorted(results) == [False, True]
    with Session(postgresql_engine) as verify:
        links = (
            verify.query(CommonFileService)
            .filter(CommonFileService.file_id == common_file_id)
            .filter(CommonFileService.service_name == "MemoryKeeper")
            .all()
        )
        assert len(links) == 1
