from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.common.database import Base


class CommonFileService(Base):
    """Service ownership link for a shared common file asset."""

    __tablename__ = "common_file_services"
    __table_args__ = (
        UniqueConstraint(
            "file_id",
            "service_name",
            name="uq_common_file_services_file_service",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    file_id = Column(
        Integer,
        ForeignKey("common_files.id"),
        nullable=False,
        index=True,
    )
    service_name = Column(String(50), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
