"""Fail-closed fixtures for destructive PostgreSQL 16 integration tests."""

from __future__ import annotations

import os
import re

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.pool import NullPool


TEST_URL_ENV = "TC_POSTGRES_INTEGRATION_URL"
CONFIRM_ENV = "TC_POSTGRES_INTEGRATION_CONFIRM"
CONFIRM_VALUE = "TC_BACKEND_DISPOSABLE_POSTGRESQL_16"
DATABASE_GUARD_TOKEN = "TC_BACKEND_POSTGRESQL_INTEGRATION_V1"

# Separate from the production migration lock. This serializes destructive test
# suites that point at the same disposable database.
TEST_SUITE_LOCK_KEY = 0x5443425047545354
_TEST_NAME = re.compile(r"(?:^|[_-])(test|testing|integration|ci|qa)(?:[_-]|$)", re.I)


def _validated_test_url() -> URL:
    raw_url = (os.environ.get(TEST_URL_ENV) or "").strip()
    if not raw_url:
        pytest.skip(
            f"{TEST_URL_ENV} is not set; PostgreSQL integration tests are opt-in"
        )
    if os.environ.get(CONFIRM_ENV) != CONFIRM_VALUE:
        pytest.fail(
            f"{CONFIRM_ENV} must exactly equal {CONFIRM_VALUE}; refusing DB access",
            pytrace=False,
        )

    try:
        url = make_url(raw_url)
    except Exception:
        pytest.fail(f"{TEST_URL_ENV} is not a valid SQLAlchemy URL", pytrace=False)

    if url.get_backend_name() != "postgresql":
        pytest.fail(f"{TEST_URL_ENV} must use PostgreSQL", pytrace=False)
    database_name = url.database or ""
    if not database_name or database_name in {"postgres", "template0", "template1"}:
        pytest.fail("refusing a system/default PostgreSQL database", pytrace=False)
    if not _TEST_NAME.search(database_name):
        pytest.fail(
            "integration database name must contain a separate test/integration/ci/qa token",
            pytrace=False,
        )
    return url


def _new_engine(url: URL) -> Engine:
    return create_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": 5,
            "options": "-csearch_path=public -cstatement_timeout=30000",
        },
    )


def _verify_server_guard(engine: Engine, expected_database: str) -> None:
    """Require an explicit marker outside public before any destructive DDL."""
    try:
        with engine.connect() as connection:
            server_version = int(
                connection.execute(text("SHOW server_version_num")).scalar_one()
            )
            actual_database = str(
                connection.execute(text("SELECT current_database()"))
                .scalar_one()
            )
            in_recovery = bool(
                connection.execute(text("SELECT pg_is_in_recovery()"))
                .scalar_one()
            )
            marker_table = connection.execute(
                text("SELECT to_regclass('tc_test_guard.authorization')")
            ).scalar_one()
            marker_count = 0
            if marker_table is not None:
                marker_count = int(
                    connection.execute(
                        text(
                            "SELECT count(*) FROM tc_test_guard.authorization "
                            "WHERE token = :token"
                        ),
                        {"token": DATABASE_GUARD_TOKEN},
                    ).scalar_one()
                )
            can_create = bool(
                connection.execute(
                    text(
                        "SELECT has_database_privilege("
                        "current_user, current_database(), 'CREATE')"
                    )
                ).scalar_one()
            )
    except Exception as exc:
        pytest.fail(
            f"PostgreSQL integration safety verification failed: {type(exc).__name__}",
            pytrace=False,
        )

    if server_version // 10000 != 16:
        pytest.fail(
            f"PostgreSQL 16 is required; detected major {server_version // 10000}",
            pytrace=False,
        )
    if actual_database != expected_database:
        pytest.fail("connected database differs from the requested test database")
    if in_recovery:
        pytest.fail("refusing to run destructive tests on a recovery/standby server")
    if marker_table is None:
        pytest.fail(
            "disposable DB authorization table is missing: "
            "tc_test_guard.authorization",
            pytrace=False,
        )
    if marker_count != 1:
        pytest.fail("disposable DB authorization token must exist exactly once")
    if not can_create:
        pytest.fail("integration role requires CREATE privilege on the disposable DB")


def _reset_public_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public AUTHORIZATION CURRENT_USER"))


@pytest.fixture(scope="session")
def postgresql_test_url() -> URL:
    """Return only a URL that passed environment and server-side guards."""
    url = _validated_test_url()
    engine = _new_engine(url)
    try:
        _verify_server_guard(engine, url.database or "")
    finally:
        engine.dispose()
    return url


@pytest.fixture(scope="session")
def postgresql_engine(postgresql_test_url: URL) -> Engine:
    """Hold a suite lock so two destructive runs cannot share one test DB."""
    engine = _new_engine(postgresql_test_url)
    lock_connection = engine.connect()
    acquired = bool(
        lock_connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"),
            {"key": TEST_SUITE_LOCK_KEY},
        ).scalar_one()
    )
    lock_connection.commit()
    if not acquired:
        lock_connection.close()
        engine.dispose()
        pytest.fail(
            "another PostgreSQL integration suite owns this disposable database",
            pytrace=False,
        )

    try:
        yield engine
    finally:
        if lock_connection.in_transaction():
            lock_connection.rollback()
        lock_connection.execute(
            text("SELECT pg_advisory_unlock(:key)"),
            {"key": TEST_SUITE_LOCK_KEY},
        )
        lock_connection.commit()
        lock_connection.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def isolated_public_schema(postgresql_engine: Engine):
    """Reset only public; the authorization marker lives in tc_test_guard."""
    _reset_public_schema(postgresql_engine)
    try:
        yield
    finally:
        _reset_public_schema(postgresql_engine)


@pytest.fixture
def migration_engine_factory(postgresql_test_url: URL):
    """Create short-lived engines matching scripts.db_migrate's NullPool use."""
    return lambda: _new_engine(postgresql_test_url)
