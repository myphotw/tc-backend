from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.sql import func

from app.common.model_base import Base
from app.common.schema_sync import migration_managed_schema_info


class AstroMultiNightFramingReference(Base):
    """Canonical framing snapshot used to reproduce a target composition."""

    __tablename__ = "astro_multi_night_framing_references"
    __table_args__ = (
        CheckConstraint(
            "reference_hour_angle_deg >= -180 "
            "AND reference_hour_angle_deg <= 180",
            name="ck_astro_multi_night_reference_hour_angle",
        ),
        CheckConstraint(
            "reference_parallactic_angle_deg >= -180 "
            "AND reference_parallactic_angle_deg <= 180",
            name="ck_astro_multi_night_reference_parallactic_angle",
        ),
        CheckConstraint(
            "reference_branch IN ('rising', 'setting')",
            name="ck_astro_multi_night_reference_branch",
        ),
        Index(
            "uq_astro_multi_night_active_target_equipment",
            "catalog_object_id",
            "equipment_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_astro_multi_night_active_captured",
            "deleted_at",
            "reference_captured_at",
            "id",
        ),
        {"info": migration_managed_schema_info()},
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    catalog_object_id = Column(String(255), nullable=False)
    reference_captured_at = Column(DateTime(timezone=True), nullable=False)
    site_id = Column(
        String(36),
        ForeignKey("astro_observation_sites.id", ondelete="RESTRICT"),
        nullable=False,
    )
    equipment_id = Column(
        String(36),
        ForeignKey("astro_equipment.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reference_hour_angle_deg = Column(Float, nullable=False)
    reference_parallactic_angle_deg = Column(Float, nullable=False)
    reference_branch = Column(String(16), nullable=False)
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
