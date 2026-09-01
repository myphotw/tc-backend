from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.sql import func

from app.common.model_base import Base


class CommonWorkerStatus(Base):
    """Worker 실행 상태 / Heartbeat 모니터링."""

    __tablename__ = "common_worker_status"

    worker_name = Column(
        String(100),
        primary_key=True,
        index=True,
    )

    status = Column(
        String(30),
        nullable=False,
        index=True,
    )

    last_started = Column(
        DateTime(timezone=True),
        nullable=True,
    )

    last_heartbeat = Column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )

    processed_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    failed_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    current_job_id = Column(
        String(100),
        nullable=True,
    )

    version = Column(
        String(50),
        nullable=True,
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
