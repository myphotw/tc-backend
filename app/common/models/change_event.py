from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Index, Integer, String, text
from sqlalchemy.sql import func

from app.common.database import Base


class CommonChangeEvent(Base):
    """Append-only event used by cursor-based client synchronization."""

    __tablename__ = "common_change_events"
    __table_args__ = (
        Index("ix_common_change_events_service_cursor", "service_name", "id"),
        Index(
            "ix_common_change_events_resource",
            "service_name",
            "resource_type",
            "resource_id",
            "id",
        ),
    )

    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    service_name = Column(String(50), nullable=False, index=True)
    resource_type = Column(String(100), nullable=False, index=True)
    resource_id = Column(String(255), nullable=False, index=True)
    operation = Column(String(20), nullable=False, index=True)
    revision = Column(Integer, nullable=True)
    tombstone = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    changed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )
