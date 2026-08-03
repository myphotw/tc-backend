from sqlalchemy import Boolean, Column, DateTime, Enum, Float, ForeignKey, Integer, String
from sqlalchemy.sql import func

from app.common.database import Base


class CommonFileTag(Base):
    """AI / User 태그. 승인(approved) 없이 현재 상태만 관리한다."""

    __tablename__ = "common_file_tags"

    id = Column(
        Integer,
        primary_key=True,
        index=True,
    )

    file_id = Column(
        Integer,
        ForeignKey("common_files.id"),
        nullable=False,
        index=True,
    )

    tag = Column(
        String(255),
        nullable=False,
        index=True,
    )

    tag_type = Column(
        Enum("AI", "ASTRO", "USER", "SYSTEM", name="common_file_tag_type"),
        nullable=False,
        index=True,
    )

    source = Column(
        Enum("AI", "USER", name="common_file_tag_source"),
        nullable=False,
        index=True,
    )

    confidence = Column(
        Float,
        nullable=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    deleted = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        index=True,
    )
