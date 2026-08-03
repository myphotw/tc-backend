from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.sql import func

from app.common.database import Base


class CommonFile(Base):
    """공통 파일 메타데이터 모델 (common_files)."""

    __tablename__ = "common_files"

    id = Column(
        Integer,
        primary_key=True,
        index=True,
    )

    file_id = Column(
        String(64),
        unique=True,
        nullable=False,
        index=True,
    )

    original_name = Column(
        String(255),
        nullable=False,
    )

    extension = Column(
        String(20),
        nullable=True,
    )

    mime_type = Column(
        String(100),
        nullable=True,
    )

    file_size = Column(
        BigInteger,
        nullable=True,
    )

    width = Column(
        Integer,
        nullable=True,
    )

    height = Column(
        Integer,
        nullable=True,
    )

    original_path = Column(
        Text,
        nullable=True,
    )

    preview_path = Column(
        Text,
        nullable=True,
    )

    thumb_path = Column(
        Text,
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
    )
