from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.sql import func

from app.common.model_base import Base


class PhotoTag(Base):
    __tablename__ = "mk_photo_tags"

    id = Column(
        Integer,
        primary_key=True,
        index=True
    )

    photo_id = Column(
        Integer,
        ForeignKey("mk_photos.id"),
        nullable=False
    )

    tag_id = Column(
        Integer,
        ForeignKey("mk_tags.id"),
        nullable=False
    )

    confidence = Column(
        Float,
        nullable=True
    )

    source = Column(
        String(50),
        nullable=False
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )
