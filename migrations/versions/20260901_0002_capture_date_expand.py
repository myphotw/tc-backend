"""Add nullable MemoryKeeper capture-date projection columns.

Revision ID: 20260901_0002
Revises: 20260831_0001
Create Date: 2026-09-01

This is an expand-only schema revision.  It deliberately does not backfill
data, add indexes, or change existing Gallery read behavior.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260901_0002"
down_revision: Union[str, Sequence[str], None] = "20260831_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "common_file_metadata",
        sa.Column(
            "original_capture_datetime",
            sa.DateTime(timezone=False),
            nullable=True,
        ),
    )

    op.add_column(
        "memorykeeper_file_states",
        sa.Column(
            "user_capture_datetime",
            sa.DateTime(timezone=False),
            nullable=True,
        ),
    )
    op.add_column(
        "memorykeeper_file_states",
        sa.Column("user_capture_precision", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "memorykeeper_file_states",
        sa.Column(
            "effective_capture_datetime",
            sa.DateTime(timezone=False),
            nullable=True,
        ),
    )
    op.add_column(
        "memorykeeper_file_states",
        sa.Column(
            "effective_capture_date",
            sa.Date(),
            sa.Computed("effective_capture_datetime::date", persisted=True),
            nullable=True,
        ),
    )
    op.add_column(
        "memorykeeper_file_states",
        sa.Column(
            "effective_capture_year",
            sa.Integer(),
            sa.Computed(
                "EXTRACT(YEAR FROM effective_capture_datetime)::integer",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "memorykeeper_file_states",
        sa.Column("date_basis", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    # Generated dependants must be removed before their source column.
    op.drop_column("memorykeeper_file_states", "effective_capture_year")
    op.drop_column("memorykeeper_file_states", "effective_capture_date")
    op.drop_column("memorykeeper_file_states", "date_basis")
    op.drop_column("memorykeeper_file_states", "effective_capture_datetime")
    op.drop_column("memorykeeper_file_states", "user_capture_precision")
    op.drop_column("memorykeeper_file_states", "user_capture_datetime")
    op.drop_column("common_file_metadata", "original_capture_datetime")
