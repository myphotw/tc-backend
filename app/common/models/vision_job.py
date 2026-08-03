from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.sql import func

from app.common.database import Base


class CommonVisionJob(Base):
    """Vision 분석 대기열 모델."""

    __tablename__ = "common_vision_jobs"

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

    priority = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        index=True,
    )

    status = Column(
        Enum(
            "WAITING",
            "PROCESSING",
            "COMPLETED",
            "FAILED",
            "SKIPPED",
            name="common_vision_job_status",
        ),
        nullable=False,
        index=True,
    )

    retry_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    vision_provider = Column(
        Enum(
            "GOOGLE",
            "AZURE",
            "AWS",
            "LOCAL",
            name="common_vision_provider",
        ),
        nullable=False,
        default="GOOGLE",
        server_default="GOOGLE",
    )

    requested_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    started_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    completed_at = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_error = Column(
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
