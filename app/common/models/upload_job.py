from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.common.database import Base


class UploadJob(Base):
    """업로드 후처리 작업 모델."""

    __tablename__ = "common_upload_jobs"

    id = Column(
        Integer,
        primary_key=True,
        index=True,
    )

    job_id = Column(
        String(36),
        unique=True,
        nullable=False,
        index=True,
    )

    source_type = Column(
        String(50),
        nullable=False,
    )

    status = Column(
        String(30),
        nullable=False,
        index=True,
    )

    incoming_path = Column(
        Text,
        nullable=False,
    )

    file_id = Column(
        String(64),
        nullable=True,
        index=True,
    )

    retry_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    error_message = Column(
        Text,
        nullable=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    started_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    completed_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )
    processing_log = Column(
        Text,
        nullable=True,
    )
