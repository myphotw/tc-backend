from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.sql import func, text

from app.common.model_base import Base


class ObservationRecord(Base):
    """AstroJournal metadata attached to a shared common file asset."""

    __tablename__ = "astro_observation_records"
    __table_args__ = (
        Index("ix_astro_observation_records_created_at", "created_at"),
        Index("ix_astro_observation_records_catalog_object_id", "catalog_object_id"),
        Index("ix_astro_observation_records_captured_at", "captured_at"),
        Index("ix_astro_observation_records_favorite", "favorite"),
        Index(
            "ix_astro_observation_records_catalog_representative",
            "catalog_object_id",
            "representative",
            "deleted_at",
        ),
        Index(
            "uq_astro_observation_records_active_representative",
            "catalog_object_id",
            unique=True,
            postgresql_where=text(
                "representative = true AND deleted_at IS NULL "
                "AND catalog_object_id IS NOT NULL"
            ),
            sqlite_where=text(
                "representative = 1 AND deleted_at IS NULL "
                "AND catalog_object_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_astro_observation_records_service_client_record_id",
            "service_name",
            "client_record_id",
            unique=True,
            postgresql_where=text("client_record_id IS NOT NULL"),
            sqlite_where=text("client_record_id IS NOT NULL"),
        ),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    file_id = Column(Integer, ForeignKey("common_files.id"), nullable=False, index=True)
    service_name = Column(
        String(50),
        nullable=False,
        default="AstroJournal",
        server_default=text("'AstroJournal'"),
        index=True,
    )
    client_record_id = Column(String(36), nullable=True)
    catalog_object_id = Column(String(255), nullable=True)
    captured_at = Column(DateTime(timezone=True), nullable=False)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    location_name = Column(String(255), nullable=True)
    equipment_id = Column(String(255), nullable=True)
    memo = Column(Text, nullable=True)
    favorite = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    representative = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    plate_solve_status = Column(
        String(30),
        nullable=False,
        default="PENDING",
        server_default=text("'PENDING'"),
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
    revision = Column(Integer, nullable=False, default=1, server_default=text("1"))
