from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.sql import func

from app.common.model_base import Base


class MemoryKeeperTagCanonicalOverride(Base):
    """User-managed projection override for one curated canonical identity."""

    __tablename__ = "mk_tag_canonical_overrides"
    __table_args__ = (
        Index("uq_mk_tag_canonical_overrides_key", "canonical_key", unique=True),
    )

    id = Column(Integer, primary_key=True, index=True)
    canonical_key = Column(String(100), nullable=False)
    memorykeeper_tag_id = Column(
        Integer,
        ForeignKey("mk_tags.id"),
        nullable=True,
        index=True,
    )
    suppressed = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        index=True,
    )
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
