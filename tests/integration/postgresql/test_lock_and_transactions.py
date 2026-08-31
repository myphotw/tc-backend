from __future__ import annotations

from alembic import command
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from scripts.db_migrate import (
    TC_BACKEND_MIGRATION_LOCK_KEY,
    run_locked_operation,
    session_advisory_lock,
)
from tests.integration.postgresql.support import (
    build_test_alembic_config,
    release_migration_lock,
    table_exists,
    try_migration_lock,
)


pytestmark = pytest.mark.postgresql_integration


def test_revision_uses_lock_owner_session_and_lock_survives_commit(
    postgresql_engine: Engine,
) -> None:
    with (
        postgresql_engine.connect() as owner,
        postgresql_engine.connect() as contender,
    ):
        owner_pid = int(owner.execute(text("SELECT pg_backend_pid()")).scalar_one())
        owner.commit()

        with session_advisory_lock(owner):
            command.upgrade(build_test_alembic_config(owner), "pgtest_0001")
            revision_pid = int(
                contender.execute(
                    text(
                        "SELECT backend_pid FROM test_revision_probe "
                        "WHERE phase = 'transactional'"
                    )
                ).scalar_one()
            )
            contender.commit()

            assert revision_pid == owner_pid
            assert not try_migration_lock(
                contender,
                TC_BACKEND_MIGRATION_LOCK_KEY,
            )

        assert try_migration_lock(contender, TC_BACKEND_MIGRATION_LOCK_KEY)
        assert release_migration_lock(contender, TC_BACKEND_MIGRATION_LOCK_KEY)


def test_pg_try_advisory_lock_is_nonblocking_and_reacquirable(
    postgresql_engine: Engine,
) -> None:
    with (
        postgresql_engine.connect() as first,
        postgresql_engine.connect() as second,
    ):
        assert try_migration_lock(first, TC_BACKEND_MIGRATION_LOCK_KEY)
        assert not try_migration_lock(second, TC_BACKEND_MIGRATION_LOCK_KEY)
        assert release_migration_lock(first, TC_BACKEND_MIGRATION_LOCK_KEY)
        assert try_migration_lock(second, TC_BACKEND_MIGRATION_LOCK_KEY)
        assert release_migration_lock(second, TC_BACKEND_MIGRATION_LOCK_KEY)


def test_autocommit_block_and_concurrent_index_keep_same_session_lock(
    postgresql_engine: Engine,
) -> None:
    with (
        postgresql_engine.connect() as owner,
        postgresql_engine.connect() as contender,
    ):
        owner_pid = int(owner.execute(text("SELECT pg_backend_pid()")).scalar_one())
        owner.commit()

        with session_advisory_lock(owner):
            command.upgrade(build_test_alembic_config(owner), "pgtest_0002")
            autocommit_pid = int(
                owner.execute(
                    text(
                        "SELECT backend_pid FROM test_revision_probe "
                        "WHERE phase = 'autocommit'"
                    )
                ).scalar_one()
            )
            index_valid = bool(
                owner.execute(
                    text(
                        "SELECT i.indisvalid "
                        "FROM pg_catalog.pg_index AS i "
                        "JOIN pg_catalog.pg_class AS c ON c.oid = i.indexrelid "
                        "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' "
                        "AND c.relname = 'ix_test_revision_probe_phase'"
                    )
                ).scalar_one()
            )
            assert autocommit_pid == owner_pid
            assert index_valid
            assert not try_migration_lock(
                contender,
                TC_BACKEND_MIGRATION_LOCK_KEY,
            )

        assert try_migration_lock(contender, TC_BACKEND_MIGRATION_LOCK_KEY)
        assert release_migration_lock(contender, TC_BACKEND_MIGRATION_LOCK_KEY)


def test_failed_revision_rolls_back_unlocks_and_preserves_primary_error(
    postgresql_engine: Engine,
) -> None:
    with (
        postgresql_engine.connect() as owner,
        postgresql_engine.connect() as contender,
    ):
        config = build_test_alembic_config(owner)
        command.upgrade(config, "pgtest_0002")

        with pytest.raises(RuntimeError, match="intentional PostgreSQL integration"):
            run_locked_operation(
                owner,
                lambda: command.upgrade(config, "head"),
            )

        assert not table_exists(contender, "test_failed_revision_probe")
        applied_revision = contender.execute(
            text("SELECT version_num FROM public.test_alembic_version")
        ).scalar_one()
        contender.commit()
        assert applied_revision == "pgtest_0002"
        assert try_migration_lock(contender, TC_BACKEND_MIGRATION_LOCK_KEY)
        assert release_migration_lock(contender, TC_BACKEND_MIGRATION_LOCK_KEY)


def test_primary_error_remains_primary_when_real_unlock_reports_not_owned(
    postgresql_engine: Engine,
) -> None:
    with (
        postgresql_engine.connect() as owner,
        postgresql_engine.connect() as contender,
    ):
        primary = ValueError("primary migration failure")

        def fail_after_releasing_lock() -> None:
            released = owner.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": TC_BACKEND_MIGRATION_LOCK_KEY},
            ).scalar_one()
            owner.commit()
            assert released is True
            raise primary

        with pytest.raises(ValueError) as caught:
            run_locked_operation(owner, fail_after_releasing_lock)

        assert caught.value is primary
        assert getattr(caught.value, "migration_unlock_error", None) is not None
        assert try_migration_lock(contender, TC_BACKEND_MIGRATION_LOCK_KEY)
        assert release_migration_lock(contender, TC_BACKEND_MIGRATION_LOCK_KEY)
