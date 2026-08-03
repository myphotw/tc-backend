from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.common.database import Base


class CommonApiUsage(Base):
    """외부 API 월별 사용량 모델."""

    __tablename__ = "common_api_usage"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "api_name",
            "year",
            "month",
            name="uq_common_api_usage_provider_api_period",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)

    provider = Column(
        Enum(
            "GOOGLE",
            "WEATHER",
            "ASTROMETRY",
            "LOCAL",
            name="common_api_usage_provider",
        ),
        nullable=False,
        index=True,
    )

    api_name = Column(
        String(50),
        nullable=False,
        index=True,
    )

    year = Column(Integer, nullable=False, index=True)
    month = Column(Integer, nullable=False, index=True)

    used_unit = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    limit_unit = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    remaining_unit = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )

    last_called_at = Column(DateTime(timezone=True), nullable=True)

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
