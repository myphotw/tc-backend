from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, String, text
from sqlalchemy.sql import func

from app.common.database import Base


class MemoryKeeperPlace(Base):
    """User-managed representative place for MemoryKeeper only."""

    __tablename__ = "memorykeeper_places"
    __table_args__ = (
        Index("ix_memorykeeper_places_active_provider", "active", "provider_place_id"),
        Index("ix_memorykeeper_places_active_canonical", "active", "canonical_name"),
        Index("ix_memorykeeper_places_active_coordinates", "active", "latitude", "longitude"),
        Index(
            "uq_memorykeeper_places_auto_dedup_key",
            "auto_dedup_key",
            unique=True,
        ),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    display_name = Column(String(200), nullable=False, index=True)
    canonical_name = Column(String(300), nullable=True, index=True)
    address = Column(String(500), nullable=True)
    postal_code = Column(String(50), nullable=True)
    country = Column(String(100), nullable=True)
    province = Column(String(100), nullable=True)
    city = Column(String(100), nullable=True)
    district = Column(String(100), nullable=True)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    radius_m = Column(Float, nullable=False, default=200.0, server_default=text("200"))
    provider_place_id = Column(String(255), nullable=True, index=True)
    category = Column(String(100), nullable=True)
    creation_source = Column(
        String(50),
        nullable=False,
        default="USER",
        server_default=text("'USER'"),
        index=True,
    )
    auto_dedup_key = Column(String(500), nullable=True)
    active = Column(Boolean, nullable=False, default=True, server_default=text("true"), index=True)
    favorite = Column(Boolean, nullable=False, default=False, server_default=text("false"), index=True)
    usage_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revision = Column(Integer, nullable=False, default=1, server_default=text("1"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True, index=True)
