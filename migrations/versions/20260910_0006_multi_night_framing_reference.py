"""Add AstroJournal multi-night framing reference.

Revision ID: 20260910_0006
Revises: 20260909_0005
Create Date: 2026-09-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260910_0006"
down_revision: Union[str, Sequence[str], None] = "20260909_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "astro_multi_night_framing_references",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("catalog_object_id", sa.String(length=255), nullable=False),
        sa.Column(
            "reference_captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("site_id", sa.String(length=36), nullable=False),
        sa.Column("equipment_id", sa.String(length=36), nullable=False),
        sa.Column("reference_hour_angle_deg", sa.Float(), nullable=False),
        sa.Column(
            "reference_parallactic_angle_deg",
            sa.Float(),
            nullable=False,
        ),
        sa.Column("reference_branch", sa.String(length=16), nullable=False),
        sa.Column(
            "revision",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "reference_hour_angle_deg >= -180 "
            "AND reference_hour_angle_deg <= 180",
            name="ck_astro_multi_night_reference_hour_angle",
        ),
        sa.CheckConstraint(
            "reference_parallactic_angle_deg >= -180 "
            "AND reference_parallactic_angle_deg <= 180",
            name="ck_astro_multi_night_reference_parallactic_angle",
        ),
        sa.CheckConstraint(
            "reference_branch IN ('rising', 'setting')",
            name="ck_astro_multi_night_reference_branch",
        ),
        sa.ForeignKeyConstraint(
            ["equipment_id"],
            ["astro_equipment.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["astro_observation_sites.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_astro_multi_night_active_captured",
        "astro_multi_night_framing_references",
        ["deleted_at", "reference_captured_at", "id"],
    )
    op.create_index(
        "uq_astro_multi_night_active_target_equipment",
        "astro_multi_night_framing_references",
        ["catalog_object_id", "equipment_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("astro_multi_night_framing_references")
