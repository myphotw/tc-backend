from __future__ import annotations

import pytest

from tests.integration.postgresql.conftest import (
    CONFIRM_ENV,
    CONFIRM_VALUE,
    TEST_URL_ENV,
    _validated_test_url,
)


def test_integration_url_is_mandatory_and_never_falls_back(monkeypatch) -> None:
    monkeypatch.delenv(TEST_URL_ENV, raising=False)
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql://ignored:ignored@ignored/tc_backend_test",
    )

    with pytest.raises(pytest.skip.Exception):
        _validated_test_url()


def test_explicit_url_without_confirmation_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv(
        TEST_URL_ENV,
        "postgresql://user:password@host/tc_backend_integration_test",
    )
    monkeypatch.delenv(CONFIRM_ENV, raising=False)

    with pytest.raises(pytest.fail.Exception):
        _validated_test_url()


@pytest.mark.parametrize(
    "database_name",
    ("postgres", "template0", "template1", "ordinary_database"),
)
def test_system_or_unmarked_database_name_is_rejected(
    database_name: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        TEST_URL_ENV,
        f"postgresql://user:password@host/{database_name}",
    )
    monkeypatch.setenv(CONFIRM_ENV, CONFIRM_VALUE)

    with pytest.raises(pytest.fail.Exception):
        _validated_test_url()
