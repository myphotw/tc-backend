from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.common.database import Base


class CommonGeocodeCache(Base):
    """Google Geocoding 결과 캐시."""

    __tablename__ = "common_geocode_cache"
    __table_args__ = (
        UniqueConstraint(
            "latitude",
            "longitude",
            name="uq_common_geocode_cache_lat_lon",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    latitude = Column(Float, nullable=False, index=True)
    longitude = Column(Float, nullable=False, index=True)

    country = Column(String(100), nullable=True)
    province = Column(String(100), nullable=True)
    city = Column(String(100), nullable=True)
    district = Column(String(100), nullable=True)
    place_name = Column(Text, nullable=True)

    provider = Column(
        String(50),
        nullable=False,
        default="GOOGLE",
        server_default="GOOGLE",
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now())
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
