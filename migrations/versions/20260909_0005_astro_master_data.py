"""Add AstroJournal observation-site and equipment master aggregates.

Revision ID: 20260909_0005
Revises: 20260906_0004
Create Date: 2026-09-09
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260909_0005"
down_revision: Union[str, Sequence[str], None] = "20260906_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "astro_equipment",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("focal_length_mm", sa.Float(), nullable=True),
        sa.Column("aperture_mm", sa.Float(), nullable=True),
        sa.Column("fov_width_degrees", sa.Float(), nullable=True),
        sa.Column("fov_height_degrees", sa.Float(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('smartTelescope', 'refractor', 'reflector', 'other')",
            name="ck_astro_equipment_kind",
        ),
        sa.CheckConstraint(
            "purpose IN ('imaging', 'visual')",
            name="ck_astro_equipment_purpose",
        ),
        sa.CheckConstraint(
            "focal_length_mm IS NULL OR focal_length_mm > 0",
            name="ck_astro_equipment_focal_length",
        ),
        sa.CheckConstraint(
            "aperture_mm IS NULL OR aperture_mm > 0",
            name="ck_astro_equipment_aperture",
        ),
        sa.CheckConstraint(
            "fov_width_degrees IS NULL OR fov_width_degrees > 0",
            name="ck_astro_equipment_fov_width",
        ),
        sa.CheckConstraint(
            "fov_height_degrees IS NULL OR fov_height_degrees > 0",
            name="ck_astro_equipment_fov_height",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_astro_equipment_active_sort",
        "astro_equipment",
        ["deleted_at", "is_active", "sort_order", "name"],
    )

    op.create_table(
        "astro_observation_sites",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("bortle", sa.Integer(), nullable=True),
        sa.Column("sqm", sa.Float(), nullable=True),
        sa.Column("brightness_grade", sa.String(length=100), nullable=True),
        sa.Column("is_favorite", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("tracking_mode", sa.String(length=16), server_default=sa.text("'altAz'"), nullable=False),
        sa.Column("default_equipment_id", sa.String(length=36), nullable=True),
        sa.Column("default_min_altitude", sa.Float(), server_default=sa.text("20"), nullable=False),
        sa.Column("default_max_altitude", sa.Float(), nullable=True),
        sa.Column("preferred_start", sa.String(length=5), nullable=True),
        sa.Column("preferred_end", sa.String(length=5), nullable=True),
        sa.Column("memo", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "latitude >= -90 AND latitude <= 90",
            name="ck_astro_observation_sites_latitude",
        ),
        sa.CheckConstraint(
            "longitude >= -180 AND longitude <= 180",
            name="ck_astro_observation_sites_longitude",
        ),
        sa.CheckConstraint(
            "bortle IS NULL OR (bortle >= 1 AND bortle <= 9)",
            name="ck_astro_observation_sites_bortle",
        ),
        sa.CheckConstraint(
            "tracking_mode IN ('altAz', 'eq')",
            name="ck_astro_observation_sites_tracking_mode",
        ),
        sa.CheckConstraint(
            "default_min_altitude >= -90 AND default_min_altitude <= 90",
            name="ck_astro_observation_sites_min_altitude",
        ),
        sa.CheckConstraint(
            "default_max_altitude IS NULL OR "
            "(default_max_altitude >= default_min_altitude "
            "AND default_max_altitude <= 90)",
            name="ck_astro_observation_sites_max_altitude",
        ),
        sa.ForeignKeyConstraint(
            ["default_equipment_id"],
            ["astro_equipment.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_astro_observation_sites_active_name",
        "astro_observation_sites",
        ["deleted_at", "name", "id"],
    )
    op.create_index(
        "ix_astro_observation_sites_default_equipment_id",
        "astro_observation_sites",
        ["default_equipment_id"],
    )

    op.create_table(
        "astro_equipment_eyepieces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("equipment_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("focal_length_mm", sa.Float(), nullable=False),
        sa.Column("afov_degrees", sa.Float(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "focal_length_mm > 0",
            name="ck_astro_equipment_eyepieces_focal_length",
        ),
        sa.CheckConstraint(
            "afov_degrees > 0",
            name="ck_astro_equipment_eyepieces_afov",
        ),
        sa.ForeignKeyConstraint(
            ["equipment_id"],
            ["astro_equipment.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_astro_equipment_eyepieces_parent_sort",
        "astro_equipment_eyepieces",
        ["equipment_id", "sort_order", "id"],
    )

    op.create_table(
        "astro_equipment_exposure_capabilities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("equipment_id", sa.String(length=36), nullable=False),
        sa.Column("tracking_mode", sa.String(length=2), nullable=False),
        sa.Column("capability_type", sa.String(length=16), nullable=False),
        sa.Column("min_seconds", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("max_seconds", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("step_seconds", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.CheckConstraint(
            "tracking_mode IN ('AZ', 'EQ')",
            name="ck_astro_equipment_exposure_tracking_mode",
        ),
        sa.CheckConstraint(
            "capability_type IN ('discrete', 'range')",
            name="ck_astro_equipment_exposure_type",
        ),
        sa.CheckConstraint(
            "(capability_type = 'discrete' "
            "AND min_seconds IS NULL AND max_seconds IS NULL "
            "AND step_seconds IS NULL) OR "
            "(capability_type = 'range' "
            "AND min_seconds > 0 AND max_seconds >= min_seconds "
            "AND step_seconds > 0)",
            name="ck_astro_equipment_exposure_shape",
        ),
        sa.ForeignKeyConstraint(
            ["equipment_id"],
            ["astro_equipment.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "equipment_id",
            "tracking_mode",
            name="uq_astro_equipment_exposure_mode",
        ),
    )
    op.create_index(
        "ix_astro_equipment_exposure_capabilities_equipment_id",
        "astro_equipment_exposure_capabilities",
        ["equipment_id"],
    )

    op.create_table(
        "astro_equipment_exposure_values",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("capability_id", sa.Integer(), nullable=False),
        sa.Column("value_seconds", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "value_seconds > 0",
            name="ck_astro_equipment_exposure_value_positive",
        ),
        sa.ForeignKeyConstraint(
            ["capability_id"],
            ["astro_equipment_exposure_capabilities.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "capability_id",
            "sort_order",
            name="uq_astro_equipment_exposure_value_order",
        ),
        sa.UniqueConstraint(
            "capability_id",
            "value_seconds",
            name="uq_astro_equipment_exposure_value",
        ),
    )
    op.create_index(
        "ix_astro_equipment_exposure_values_capability_id",
        "astro_equipment_exposure_values",
        ["capability_id"],
    )

    op.create_table(
        "astro_observation_site_horizon_points",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("observation_site_id", sa.String(length=36), nullable=False),
        sa.Column("azimuth", sa.Float(), nullable=False),
        sa.Column("min_altitude", sa.Float(), nullable=False),
        sa.Column("max_altitude", sa.Float(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("source", sa.String(length=20), server_default=sa.text("'manual'"), nullable=False),
        sa.CheckConstraint(
            "azimuth >= 0 AND azimuth < 360",
            name="ck_astro_observation_site_horizon_azimuth",
        ),
        sa.CheckConstraint(
            "min_altitude >= -90 AND min_altitude <= 90",
            name="ck_astro_observation_site_horizon_min_altitude",
        ),
        sa.CheckConstraint(
            "max_altitude IS NULL OR "
            "(max_altitude >= min_altitude AND max_altitude <= 90)",
            name="ck_astro_observation_site_horizon_max_altitude",
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'camera_scan', 'photo_import', 'video_import')",
            name="ck_astro_observation_site_horizon_source",
        ),
        sa.ForeignKeyConstraint(
            ["observation_site_id"],
            ["astro_observation_sites.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "observation_site_id",
            "azimuth",
            name="uq_astro_observation_site_horizon_azimuth",
        ),
    )
    op.create_index(
        "ix_astro_observation_site_horizon_parent_sort",
        "astro_observation_site_horizon_points",
        ["observation_site_id", "sort_order", "id"],
    )

    op.create_table(
        "astro_observation_site_blocked_azimuth_ranges",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("observation_site_id", sa.String(length=36), nullable=False),
        sa.Column("start_azimuth", sa.Float(), nullable=False),
        sa.Column("end_azimuth", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("source", sa.String(length=20), server_default=sa.text("'manual'"), nullable=False),
        sa.CheckConstraint(
            "start_azimuth >= 0 AND start_azimuth < 360",
            name="ck_astro_observation_site_blocked_start",
        ),
        sa.CheckConstraint(
            "end_azimuth >= 0 AND end_azimuth < 360",
            name="ck_astro_observation_site_blocked_end",
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'camera_scan', 'photo_import', 'video_import')",
            name="ck_astro_observation_site_blocked_source",
        ),
        sa.ForeignKeyConstraint(
            ["observation_site_id"],
            ["astro_observation_sites.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_astro_observation_site_blocked_parent",
        "astro_observation_site_blocked_azimuth_ranges",
        ["observation_site_id", "start_azimuth", "id"],
    )


def downgrade() -> None:
    op.drop_table("astro_observation_site_blocked_azimuth_ranges")
    op.drop_table("astro_observation_site_horizon_points")
    op.drop_table("astro_equipment_exposure_values")
    op.drop_table("astro_equipment_exposure_capabilities")
    op.drop_table("astro_equipment_eyepieces")
    op.drop_table("astro_observation_sites")
    op.drop_table("astro_equipment")
