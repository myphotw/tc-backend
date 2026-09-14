"""Add MemoryKeeper photo final-category projection.

Revision ID: 20260914_0007
Revises: 20260910_0006
Create Date: 2026-09-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260914_0007"
down_revision: Union[str, Sequence[str], None] = "20260910_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL 16 can add constant-default columns without rewriting existing
    # rows.  The defaults preserve the legacy meaning: every existing photo is
    # NORMAL with category revision zero.
    op.add_column(
        "memorykeeper_file_states",
        sa.Column(
            "photo_category",
            sa.String(length=16),
            server_default=sa.text("'NORMAL'"),
            nullable=False,
        ),
    )
    op.add_column(
        "memorykeeper_file_states",
        sa.Column(
            "photo_category_revision",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_memorykeeper_file_states_photo_category",
        "memorykeeper_file_states",
        "photo_category IN ('NORMAL', 'DAILY')",
    )


def downgrade() -> None:
    # Downgrade discards user DAILY classifications and their revisions.
    op.drop_constraint(
        "ck_memorykeeper_file_states_photo_category",
        "memorykeeper_file_states",
        type_="check",
    )
    op.drop_column("memorykeeper_file_states", "photo_category_revision")
    op.drop_column("memorykeeper_file_states", "photo_category")
