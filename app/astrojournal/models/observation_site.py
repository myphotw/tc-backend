from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.sql import func

from app.common.model_base import Base
from app.common.schema_sync import migration_managed_schema_info


class AstroObservationSite(Base):
    """Canonical AstroJournal observation-site aggregate root."""

    __tablename__ = "astro_observation_sites"
    __table_args__ = (
        CheckConstraint(
            "latitude >= -90 AND latitude <= 90",
            name="ck_astro_observation_sites_latitude",
        ),
        CheckConstraint(
            "longitude >= -180 AND longitude <= 180",
            name="ck_astro_observation_sites_longitude",
        ),
        CheckConstraint(
            "bortle IS NULL OR (bortle >= 1 AND bortle <= 9)",
            name="ck_astro_observation_sites_bortle",
        ),
        CheckConstraint(
            "tracking_mode IN ('altAz', 'eq')",
            name="ck_astro_observation_sites_tracking_mode",
        ),
        CheckConstraint(
            "default_min_altitude >= -90 AND default_min_altitude <= 90",
            name="ck_astro_observation_sites_min_altitude",
        ),
        CheckConstraint(
            "default_max_altitude IS NULL OR "
            "(default_max_altitude >= default_min_altitude "
            "AND default_max_altitude <= 90)",
            name="ck_astro_observation_sites_max_altitude",
        ),
        Index(
            "ix_astro_observation_sites_active_name",
            "deleted_at",
            "name",
            "id",
        ),
        Index(
            "ix_astro_observation_sites_default_equipment_id",
            "default_equipment_id",
        ),
        {"info": migration_managed_schema_info()},
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(200), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    address = Column(String(500), nullable=True)
    bortle = Column(Integer, nullable=True)
    sqm = Column(Float, nullable=True)
    brightness_grade = Column(String(100), nullable=True)
    is_favorite = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    tracking_mode = Column(
        String(16),
        nullable=False,
        default="altAz",
        server_default=text("'altAz'"),
    )
    default_equipment_id = Column(
        String(36),
        ForeignKey("astro_equipment.id", ondelete="SET NULL"),
        nullable=True,
    )
    default_min_altitude = Column(
        Float,
        nullable=False,
        default=20.0,
        server_default=text("20"),
    )
    default_max_altitude = Column(Float, nullable=True)
    preferred_start = Column(String(5), nullable=True)
    preferred_end = Column(String(5), nullable=True)
    memo = Column(Text, nullable=False, default="", server_default=text("''"))
    revision = Column(Integer, nullable=False, default=1, server_default=text("1"))
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class AstroObservationSiteHorizonPoint(Base):
    """Ordered horizon point owned by one observation-site aggregate."""

    __tablename__ = "astro_observation_site_horizon_points"
    __table_args__ = (
        UniqueConstraint(
            "observation_site_id",
            "azimuth",
            name="uq_astro_observation_site_horizon_azimuth",
        ),
        CheckConstraint(
            "azimuth >= 0 AND azimuth < 360",
            name="ck_astro_observation_site_horizon_azimuth",
        ),
        CheckConstraint(
            "min_altitude >= -90 AND min_altitude <= 90",
            name="ck_astro_observation_site_horizon_min_altitude",
        ),
        CheckConstraint(
            "max_altitude IS NULL OR "
            "(max_altitude >= min_altitude AND max_altitude <= 90)",
            name="ck_astro_observation_site_horizon_max_altitude",
        ),
        CheckConstraint(
            "source IN ('manual', 'camera_scan', 'photo_import', 'video_import')",
            name="ck_astro_observation_site_horizon_source",
        ),
        Index(
            "ix_astro_observation_site_horizon_parent_sort",
            "observation_site_id",
            "sort_order",
            "id",
        ),
        {"info": migration_managed_schema_info()},
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    observation_site_id = Column(
        String(36),
        ForeignKey("astro_observation_sites.id", ondelete="CASCADE"),
        nullable=False,
    )
    azimuth = Column(Float, nullable=False)
    min_altitude = Column(Float, nullable=False)
    max_altitude = Column(Float, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0, server_default=text("0"))
    source = Column(
        String(20),
        nullable=False,
        default="manual",
        server_default=text("'manual'"),
    )


class AstroObservationSiteBlockedRange(Base):
    """Circular blocked-azimuth range owned by an observation site."""

    __tablename__ = "astro_observation_site_blocked_azimuth_ranges"
    __table_args__ = (
        CheckConstraint(
            "start_azimuth >= 0 AND start_azimuth < 360",
            name="ck_astro_observation_site_blocked_start",
        ),
        CheckConstraint(
            "end_azimuth >= 0 AND end_azimuth < 360",
            name="ck_astro_observation_site_blocked_end",
        ),
        CheckConstraint(
            "source IN ('manual', 'camera_scan', 'photo_import', 'video_import')",
            name="ck_astro_observation_site_blocked_source",
        ),
        Index(
            "ix_astro_observation_site_blocked_parent",
            "observation_site_id",
            "start_azimuth",
            "id",
        ),
        {"info": migration_managed_schema_info()},
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    observation_site_id = Column(
        String(36),
        ForeignKey("astro_observation_sites.id", ondelete="CASCADE"),
        nullable=False,
    )
    start_azimuth = Column(Float, nullable=False)
    end_azimuth = Column(Float, nullable=False)
    reason = Column(String(500), nullable=True)
    source = Column(
        String(20),
        nullable=False,
        default="manual",
        server_default=text("'manual'"),
    )
