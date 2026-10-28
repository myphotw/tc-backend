"""Add generalized capture capability to AstroJournal equipment.

Revision ID: 20261028_0011
Revises: 20261021_0010
Create Date: 2026-10-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20261028_0011"
down_revision: Union[str, Sequence[str], None] = "20261021_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "astro_equipment",
        sa.Column(
            "capture_capability",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("astro_equipment", "capture_capability")
