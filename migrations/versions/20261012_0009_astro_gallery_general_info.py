"""Add Astro Gallery general-information synchronization fields.

Revision ID: 20261012_0009
Revises: 20260925_0008
Create Date: 2026-10-12

This is an additive expand migration. Existing free-form equipment_id values
are deliberately left untouched and no data backfill is performed.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261012_0009"
down_revision: Union[str, Sequence[str], None] = "20260925_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "astro_observation_records",
        sa.Column("equipment_ref_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "astro_observation_records",
        sa.Column("equipment_name_snapshot", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "astro_observation_records",
        sa.Column("integration_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "astro_observation_records",
        sa.Column("address", sa.String(length=500), nullable=True),
    )
    op.create_foreign_key(
        "fk_astro_observation_records_equipment_ref_id",
        "astro_observation_records",
        "astro_equipment",
        ["equipment_ref_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_astro_observation_records_integration_seconds",
        "astro_observation_records",
        "integration_seconds IS NULL OR integration_seconds >= 0",
    )
    op.create_index(
        "ix_astro_observation_records_equipment_ref_id",
        "astro_observation_records",
        ["equipment_ref_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_astro_observation_records_equipment_ref_id",
        table_name="astro_observation_records",
    )
    op.drop_constraint(
        "ck_astro_observation_records_integration_seconds",
        "astro_observation_records",
        type_="check",
    )
    op.drop_constraint(
        "fk_astro_observation_records_equipment_ref_id",
        "astro_observation_records",
        type_="foreignkey",
    )
    op.drop_column("astro_observation_records", "address")
    op.drop_column("astro_observation_records", "integration_seconds")
    op.drop_column("astro_observation_records", "equipment_name_snapshot")
    op.drop_column("astro_observation_records", "equipment_ref_id")
