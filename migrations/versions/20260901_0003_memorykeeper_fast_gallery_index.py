"""Add the MemoryKeeper fast Gallery keyset index.

Revision ID: 20260901_0003
Revises: 20260901_0002
Create Date: 2026-09-01

The partial index is intentionally built concurrently: fast-read rollout must
not block photo writes while the MemoryKeeper catalog grows.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260901_0003"
down_revision: Union[str, Sequence[str], None] = "20260901_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "ix_memorykeeper_file_states_effective_capture_desc"


def upgrade() -> None:
    # PostgreSQL forbids CREATE INDEX CONCURRENTLY in a transaction block.
    with op.get_context().autocommit_block():
        op.create_index(
            INDEX_NAME,
            "memorykeeper_file_states",
            ["effective_capture_datetime", "file_id"],
            unique=False,
            postgresql_where=sa.text("effective_capture_datetime IS NOT NULL"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX_NAME,
            table_name="memorykeeper_file_states",
            postgresql_concurrently=True,
        )
