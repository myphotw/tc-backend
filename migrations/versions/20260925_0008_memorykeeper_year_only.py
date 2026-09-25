"""Add MemoryKeeper YEAR-only capture-date projection fields.

Revision ID: 20260925_0008
Revises: 20260914_0007
Create Date: 2026-09-25

This is an expand migration. The existing generated effective_capture_year is
kept intact for rollout compatibility while application reads move to the
ordinary effective_capture_year_v2 projection. A later contract migration may
remove the legacy generated column after every process uses the v2 field.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260925_0008"
down_revision: Union[str, Sequence[str], None] = "20260914_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "memorykeeper_file_states",
        sa.Column("source_capture_year", sa.Integer(), nullable=True),
    )
    op.add_column(
        "memorykeeper_file_states",
        sa.Column("source_capture_year_basis", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "memorykeeper_file_states",
        sa.Column("effective_capture_year_v2", sa.Integer(), nullable=True),
    )
    op.add_column(
        "memorykeeper_file_states",
        sa.Column("effective_capture_precision", sa.String(length=16), nullable=True),
    )

    # Preserve every existing projection exactly. Source-year reconciliation is
    # intentionally a later, separately controlled data operation.
    op.execute(
        sa.text(
            """
            UPDATE memorykeeper_file_states
            SET effective_capture_year_v2 = effective_capture_year,
                effective_capture_precision = CASE
                    WHEN effective_capture_datetime IS NULL THEN NULL
                    WHEN date_basis = 'USER' THEN
                        COALESCE(user_capture_precision, 'DATE')
                    ELSE 'DATETIME'
                END
            """
        )
    )


def downgrade() -> None:
    op.drop_column("memorykeeper_file_states", "effective_capture_precision")
    op.drop_column("memorykeeper_file_states", "effective_capture_year_v2")
    op.drop_column("memorykeeper_file_states", "source_capture_year_basis")
    op.drop_column("memorykeeper_file_states", "source_capture_year")
