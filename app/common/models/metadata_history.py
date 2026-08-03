from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from app.common.database import Base


class CommonMetadataHistory(Base):
    """파일 메타데이터 변경 이력."""

    __tablename__ = "common_metadata_history"

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

    field_name = Column(
        String(100),
        nullable=False,
        index=True,
    )

    old_value = Column(
        Text,
        nullable=True,
    )

    new_value = Column(
        Text,
        nullable=True,
    )

    source = Column(
        String(50),
        nullable=False,
        index=True,
    )

    priority = Column(
        Integer,
        nullable=False,
    )

    modified_by = Column(
        String(100),
        nullable=True,
    )

    approved = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
