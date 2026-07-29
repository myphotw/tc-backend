from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func

from app.common.database import Base


class Setting(Base):
    __tablename__ = "common_settings"

    id = Column(
        Integer,
        primary_key=True,
        index=True
    )

    category = Column(
        String(100),
        nullable=False
    )

    setting_key = Column(
        String(100),
        nullable=False,
        unique=True
    )

    setting_value = Column(
        String(500),
        nullable=False
    )

    description = Column(
        String(500),
        nullable=True
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )

    updated_at = Column(
        DateTime(timezone=True),
        onupdate=func.now()
    )