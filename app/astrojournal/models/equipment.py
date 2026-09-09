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
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.sql import func

from app.common.model_base import Base
from app.common.schema_sync import migration_managed_schema_info


class AstroEquipment(Base):
    """Canonical AstroJournal equipment aggregate root."""

    __tablename__ = "astro_equipment"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('smartTelescope', 'refractor', 'reflector', 'other')",
            name="ck_astro_equipment_kind",
        ),
        CheckConstraint(
            "purpose IN ('imaging', 'visual')",
            name="ck_astro_equipment_purpose",
        ),
        CheckConstraint(
            "focal_length_mm IS NULL OR focal_length_mm > 0",
            name="ck_astro_equipment_focal_length",
        ),
        CheckConstraint(
            "aperture_mm IS NULL OR aperture_mm > 0",
            name="ck_astro_equipment_aperture",
        ),
        CheckConstraint(
            "fov_width_degrees IS NULL OR fov_width_degrees > 0",
            name="ck_astro_equipment_fov_width",
        ),
        CheckConstraint(
            "fov_height_degrees IS NULL OR fov_height_degrees > 0",
            name="ck_astro_equipment_fov_height",
        ),
        Index(
            "ix_astro_equipment_active_sort",
            "deleted_at",
            "is_active",
            "sort_order",
            "name",
        ),
        {"info": migration_managed_schema_info()},
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(200), nullable=False)
    kind = Column(String(32), nullable=False)
    purpose = Column(String(16), nullable=False)
    is_active = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    focal_length_mm = Column(Float, nullable=True)
    aperture_mm = Column(Float, nullable=True)
    fov_width_degrees = Column(Float, nullable=True)
    fov_height_degrees = Column(Float, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0, server_default=text("0"))
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


class AstroEquipmentEyepiece(Base):
    """Eyepiece child owned by one equipment aggregate."""

    __tablename__ = "astro_equipment_eyepieces"
    __table_args__ = (
        CheckConstraint(
            "focal_length_mm > 0",
            name="ck_astro_equipment_eyepieces_focal_length",
        ),
        CheckConstraint(
            "afov_degrees > 0",
            name="ck_astro_equipment_eyepieces_afov",
        ),
        Index(
            "ix_astro_equipment_eyepieces_parent_sort",
            "equipment_id",
            "sort_order",
            "id",
        ),
        {"info": migration_managed_schema_info()},
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    equipment_id = Column(
        String(36),
        ForeignKey("astro_equipment.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(200), nullable=False)
    focal_length_mm = Column(Float, nullable=False)
    afov_degrees = Column(Float, nullable=False)
    sort_order = Column(Integer, nullable=False, default=0, server_default=text("0"))


class AstroEquipmentExposureCapability(Base):
    """AZ or EQ exposure-capability header for one equipment aggregate."""

    __tablename__ = "astro_equipment_exposure_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "equipment_id",
            "tracking_mode",
            name="uq_astro_equipment_exposure_mode",
        ),
        CheckConstraint(
            "tracking_mode IN ('AZ', 'EQ')",
            name="ck_astro_equipment_exposure_tracking_mode",
        ),
        CheckConstraint(
            "capability_type IN ('discrete', 'range')",
            name="ck_astro_equipment_exposure_type",
        ),
        CheckConstraint(
            "(capability_type = 'discrete' "
            "AND min_seconds IS NULL AND max_seconds IS NULL "
            "AND step_seconds IS NULL) OR "
            "(capability_type = 'range' "
            "AND min_seconds > 0 AND max_seconds >= min_seconds "
            "AND step_seconds > 0)",
            name="ck_astro_equipment_exposure_shape",
        ),
        {"info": migration_managed_schema_info()},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    equipment_id = Column(
        String(36),
        ForeignKey("astro_equipment.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tracking_mode = Column(String(2), nullable=False)
    capability_type = Column(String(16), nullable=False)
    min_seconds = Column(Numeric(12, 6), nullable=True)
    max_seconds = Column(Numeric(12, 6), nullable=True)
    step_seconds = Column(Numeric(12, 6), nullable=True)


class AstroEquipmentExposureValue(Base):
    """One exact discrete exposure value owned by a capability header."""

    __tablename__ = "astro_equipment_exposure_values"
    __table_args__ = (
        UniqueConstraint(
            "capability_id",
            "value_seconds",
            name="uq_astro_equipment_exposure_value",
        ),
        UniqueConstraint(
            "capability_id",
            "sort_order",
            name="uq_astro_equipment_exposure_value_order",
        ),
        CheckConstraint(
            "value_seconds > 0",
            name="ck_astro_equipment_exposure_value_positive",
        ),
        {"info": migration_managed_schema_info()},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    capability_id = Column(
        Integer,
        ForeignKey(
            "astro_equipment_exposure_capabilities.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    value_seconds = Column(Numeric(12, 6), nullable=False)
    sort_order = Column(Integer, nullable=False)
