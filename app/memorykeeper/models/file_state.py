from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.sql import func

from app.common.database import Base


class MemoryKeeperFileState(Base):
    """MemoryKeeper-only writable state for a shared CommonFile."""

    __tablename__ = "memorykeeper_file_states"
    __table_args__ = (Index("ix_memorykeeper_file_states_favorite", "favorite"),)

    file_id = Column(Integer, ForeignKey("common_files.id"), primary_key=True)
    favorite = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    memo = Column(Text, nullable=True)
    revision = Column(Integer, nullable=False, default=0, server_default="0")
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
