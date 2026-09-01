from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.sql import func

from app.common.model_base import Base


class MemoryKeeperFileTagSuppression(Base):
    """Hide one curated canonical identity for one MemoryKeeper file."""

    __tablename__ = "mk_file_tag_suppressions"
    __table_args__ = (
        Index(
            "uq_mk_file_tag_suppressions_file_canonical",
            "file_id",
            "canonical_key",
            unique=True,
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    file_id = Column(
        Integer,
        ForeignKey("common_files.id"),
        nullable=False,
        index=True,
    )
    canonical_key = Column(String(100), nullable=False, index=True)
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
