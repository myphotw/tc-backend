"""Add canonical WCS storage to AstroJournal Plate Solve jobs.

Revision ID: 20260906_0004
Revises: 20260901_0003
Create Date: 2026-09-06
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260906_0004"
down_revision: Union[str, Sequence[str], None] = "20260901_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "astro_plate_solve_jobs",
        sa.Column("wcs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("astro_plate_solve_jobs", "wcs")
