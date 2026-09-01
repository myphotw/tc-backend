from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.sql import func

from app.common.model_base import Base
from app.common.schema_sync import migration_managed_schema_info


class MemoryKeeperFileState(Base):
    """MemoryKeeper-only writable state for a shared CommonFile."""

    __tablename__ = "memorykeeper_file_states"
    __table_args__ = (
        Index("ix_memorykeeper_file_states_favorite", "favorite"),
        # This is created only by Alembic revision 20260901_0003.  Startup
        # schema sync excludes migration-owned indexes from its DDL projection.
        Index(
            "ix_memorykeeper_file_states_effective_capture_desc",
            "effective_capture_datetime",
            "file_id",
            postgresql_where=text("effective_capture_datetime IS NOT NULL"),
            info=migration_managed_schema_info(),
        ),
    )

    file_id = Column(Integer, ForeignKey("common_files.id"), primary_key=True)
    favorite = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    memo = Column(Text, nullable=True)
    revision = Column(Integer, nullable=False, default=0, server_default="0")
    # MemoryKeeper-only capture-date projection.  The nullable expand migration
    # adds these fields; dual-write/backfill and read-path adoption follow later.
    user_capture_datetime = Column(
        DateTime(timezone=False),
        nullable=True,
        info=migration_managed_schema_info(),
    )
    user_capture_precision = Column(
        String(16),
        nullable=True,
        info=migration_managed_schema_info(),
    )
    effective_capture_datetime = Column(
        DateTime(timezone=False),
        nullable=True,
        info=migration_managed_schema_info(),
    )
    effective_capture_date = Column(
        Date(),
        server_default=FetchedValue(),
        server_onupdate=FetchedValue(),
        nullable=True,
        info=migration_managed_schema_info(),
    )
    effective_capture_year = Column(
        Integer,
        server_default=FetchedValue(),
        server_onupdate=FetchedValue(),
        nullable=True,
        info=migration_managed_schema_info(),
    )
    date_basis = Column(
        String(16),
        nullable=True,
        info=migration_managed_schema_info(),
    )
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
