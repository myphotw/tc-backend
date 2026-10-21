"""Add the shared AstroJournal catalog favorite set.

Revision ID: 20261021_0010
Revises: 20261012_0009
Create Date: 2026-10-21
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261021_0010"
down_revision: Union[str, Sequence[str], None] = "20261012_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "astro_catalog_favorites",
        sa.Column("catalog_object_id", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("catalog_object_id"),
    )


def downgrade() -> None:
    op.drop_table("astro_catalog_favorites")
