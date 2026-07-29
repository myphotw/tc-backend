from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func

from app.common.database import Base


class ApiKey(Base):
    __tablename__ = "common_api_keys"

    id = Column(Integer, primary_key=True, index=True)

    service_name = Column(
        String(100),
        nullable=False,
        unique=True
    )

    api_key = Column(
        String(500),
        nullable=False
    )

    description = Column(
        String(500),
        nullable=True
    )

    enabled = Column(
        Boolean,
        default=True
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    updated_at = Column(
        DateTime(timezone=True),
        onupdate=func.now()
    )