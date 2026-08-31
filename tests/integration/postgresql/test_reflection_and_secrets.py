from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine import Engine, URL
from sqlalchemy.pool import NullPool

from migrations.baseline import _type_family, evaluate_baseline_fingerprint
from scripts.db_migrate import main
from tests.integration.postgresql.support import create_legacy_schema


pytestmark = pytest.mark.postgresql_integration


def test_baseline_fingerprint_matches_real_postgresql_reflection(
    postgresql_engine: Engine,
) -> None:
    create_legacy_schema(postgresql_engine)

    reflected = inspect(postgresql_engine)
    assert evaluate_baseline_fingerprint(reflected) == ()

    files = {
        column["name"]: column
        for column in reflected.get_columns("common_files", schema="public")
    }
    events = {
        column["name"]: column
        for column in reflected.get_columns(
            "common_change_events",
            schema="public",
        )
    }
    metadata = {
        column["name"]: column
        for column in reflected.get_columns(
            "common_file_metadata",
            schema="public",
        )
    }

    assert isinstance(files["id"]["type"], Integer)
    assert isinstance(events["id"]["type"], BigInteger)
    assert isinstance(files["deleted"]["type"], Boolean)
    assert isinstance(files["file_id"]["type"], String)
    assert files["file_id"]["type"].length == 64
    assert files["file_id"]["nullable"] is False
    assert isinstance(metadata["datetime_original"]["type"], DateTime)
    assert metadata["datetime_original"]["type"].timezone is True


def test_postgresql_reflects_timezones_keys_and_schema_qualified_fk(
    postgresql_engine: Engine,
) -> None:
    with postgresql_engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE public.reflection_probe (
                    id BIGINT PRIMARY KEY,
                    integer_value INTEGER NOT NULL,
                    code VARCHAR(37) NOT NULL UNIQUE,
                    enabled BOOLEAN NOT NULL,
                    aware_at TIMESTAMPTZ NOT NULL,
                    naive_at TIMESTAMP WITHOUT TIME ZONE NULL,
                    parent_id BIGINT NULL,
                    CONSTRAINT fk_reflection_probe_parent
                        FOREIGN KEY (parent_id)
                        REFERENCES public.reflection_probe(id)
                )
                """
            )
        )

    reflected = inspect(postgresql_engine)
    columns = {
        column["name"]: column
        for column in reflected.get_columns("reflection_probe", schema="public")
    }
    assert _type_family(columns["id"]["type"]) == "big_integer"
    assert _type_family(columns["integer_value"]["type"]) == "integer"
    assert _type_family(columns["enabled"]["type"]) == "boolean"
    assert _type_family(columns["code"]["type"]) == "string"
    assert columns["code"]["type"].length == 37
    assert columns["integer_value"]["nullable"] is False
    assert columns["parent_id"]["nullable"] is True
    assert columns["aware_at"]["type"].timezone is True
    assert columns["naive_at"]["type"].timezone is False

    primary_key = reflected.get_pk_constraint(
        "reflection_probe",
        schema="public",
    )
    assert primary_key["constrained_columns"] == ["id"]
    unique_columns = {
        tuple(constraint.get("column_names") or ())
        for constraint in reflected.get_unique_constraints(
            "reflection_probe",
            schema="public",
        )
    }
    assert ("code",) in unique_columns
    foreign_keys = reflected.get_foreign_keys(
        "reflection_probe",
        schema="public",
        postgresql_ignore_search_path=True,
    )
    assert any(
        foreign_key.get("constrained_columns") == ["parent_id"]
        and foreign_key.get("referred_schema") == "public"
        and foreign_key.get("referred_table") == "reflection_probe"
        and foreign_key.get("referred_columns") == ["id"]
        for foreign_key in foreign_keys
    )


def test_real_connection_failure_is_masked_by_wrapper(
    postgresql_test_url: URL,
    caplog,
    capsys,
) -> None:
    original_password = postgresql_test_url.password or ""
    if not original_password:
        pytest.skip("test URL has no password to exercise authentication masking")

    bad_password = "TCIntegrationDeliberatelyWrongPassword_9f31"
    bad_url = postgresql_test_url.set(password=bad_password)
    connect_args = {"connect_timeout": 3, "options": "-csearch_path=public"}

    probe_engine = create_engine(
        bad_url,
        poolclass=NullPool,
        connect_args=connect_args,
    )
    try:
        with probe_engine.connect():
            pytest.skip(
                "server authentication accepts the wrong password; masking path unavailable"
            )
    except Exception:
        pass
    finally:
        probe_engine.dispose()

    failing_engine = create_engine(
        bad_url,
        poolclass=NullPool,
        connect_args=connect_args,
    )
    with (
        patch("scripts.db_migrate.create_migration_engine", return_value=failing_engine),
        patch(
            "scripts.db_migrate.configured_secrets",
            return_value=(original_password, bad_password),
        ),
    ):
        exit_code = main(["status"])

    assert exit_code == 1
    captured = capsys.readouterr()
    rendered = captured.out + captured.err + caplog.text
    sensitive_values = (
        original_password,
        bad_password,
        bad_url.render_as_string(hide_password=False),
        postgresql_test_url.render_as_string(hide_password=False),
    )
    if any(value and value in rendered for value in sensitive_values):
        pytest.fail("wrapper or captured logger output exposed a database credential", pytrace=False)
