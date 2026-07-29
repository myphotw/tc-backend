from sqlalchemy import Column, Integer, String, DateTime, BigInteger
from sqlalchemy.sql import func

from app.common.database import Base


class Photo(Base):
    __tablename__ = "mk_photos"

    id = Column(
        Integer,
        primary_key=True,
        index=True
    )

    file_path = Column(
        String(1000),
        nullable=False
    )

    file_name = Column(
        String(500),
        nullable=False
    )

    file_size = Column(
        BigInteger,
        nullable=True
    )

    taken_at = Column(
        DateTime(timezone=True),
        nullable=True
    )

    latitude = Column(
        String(50),
        nullable=True
    )

    longitude = Column(
        String(50),
        nullable=True
    )

    location_name = Column(
        String(200),
        nullable=True
    )

    camera_model = Column(
        String(200),
        nullable=True
    )

    lens_model = Column(
        String(200),
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