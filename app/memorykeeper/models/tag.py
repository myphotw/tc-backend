from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func

from app.common.database import Base


class Tag(Base):
    __tablename__ = "mk_tags"

    id = Column(
        Integer,
        primary_key=True,
        index=True
    )

    tag_name = Column(
        String(100),
        nullable=False,
        unique=True
    )

    tag_type = Column(
        String(50),
        nullable=False
    )

    source = Column(
        String(50),
        nullable=False
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )