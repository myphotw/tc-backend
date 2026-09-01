from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func, text

from app.common.model_base import Base


class AstroPlateSolveJob(Base):
    """Persistent AstroJournal Plate Solve queue row."""

    __tablename__ = "astro_plate_solve_jobs"
    __table_args__ = (
        UniqueConstraint(
            "common_file_id",
            name="uq_astro_plate_solve_jobs_common_file_id",
        ),
        Index("ix_astro_plate_solve_jobs_status_created", "status", "created_at"),
        Index("ix_astro_plate_solve_jobs_lease", "status", "lease_expires_at"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    common_file_id = Column(
        Integer,
        ForeignKey("common_files.id"),
        nullable=False,
    )
    observation_record_id = Column(
        String(36),
        ForeignKey("astro_observation_records.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status = Column(
        String(20),
        nullable=False,
        default="WAITING",
        server_default=text("'WAITING'"),
        index=True,
    )
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    provider = Column(
        String(50),
        nullable=False,
        default="astrometry.net",
        server_default=text("'astrometry.net'"),
    )
    provider_submission_id = Column(Integer, nullable=True)
    provider_job_id = Column(Integer, nullable=True)
    worker_id = Column(String(100), nullable=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)

    ra = Column(Float, nullable=True)
    dec = Column(Float, nullable=True)
    rotation = Column(Float, nullable=True)
    pixel_scale = Column(Float, nullable=True)
    field_width = Column(Float, nullable=True)
    field_height = Column(Float, nullable=True)
    parity = Column(Float, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
