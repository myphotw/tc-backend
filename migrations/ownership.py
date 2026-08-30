"""Fail-closed Alembic ownership filters.

Autogenerate is not exposed as an operational command in phase 1. This module
only prepares the ownership filter for a future, reviewed autogenerate flow.
"""

from __future__ import annotations

from sqlalchemy import Table

from app.common.schema_sync import (
    bootstrap_managed_tables,
    is_migration_managed,
)


def include_migration_managed_object(
    schema_object: object,
    name: str | None,
    object_type: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    """Include only explicitly migration-owned model objects.

    Reflected-only objects are always excluded. This prevents Alembic from
    interpreting a bootstrap-managed legacy object as something to drop.
    Constraints remain excluded until the ownership policy explicitly supports
    them.
    """
    del name  # Alembic callback compatibility; ownership is model metadata based.

    model_object = compare_to if reflected else schema_object
    if model_object is None:
        return False

    if object_type == "table":
        if not isinstance(model_object, Table):
            return False
        bootstrap_tables = set(bootstrap_managed_tables(model_object.metadata))
        return model_object not in bootstrap_tables

    if object_type in {"column", "index"}:
        return is_migration_managed(model_object)

    # The current schema_sync ownership boundary covers Table/Column/Index.
    # Constraints must stay manual and fail-closed until that policy expands.
    return False
