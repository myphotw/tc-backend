from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, text
from sqlalchemy.sql import func

from app.common.model_base import Base


class Tag(Base):
    """MemoryKeeper user-managed tag catalog."""

    __tablename__ = "mk_tags"
    __table_args__ = (
        Index("uq_mk_tags_normalized_name", "normalized_name", unique=True),
    )

    id = Column(Integer, primary_key=True, index=True)
    tag_name = Column(String(100), nullable=False, unique=True)
    normalized_name = Column(String(100), nullable=True)
    tag_type = Column(String(50), nullable=False, default="USER", server_default="USER")
    source = Column(String(50), nullable=False, default="USER", server_default="USER")
    favorite = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        index=True,
    )
    revision = Column(Integer, nullable=False, default=1, server_default="1")
    deleted = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
