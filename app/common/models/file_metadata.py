from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.common.model_base import Base
from app.common.schema_sync import migration_managed_schema_info


class CommonFileMetadata(Base):
    """프로그램에서 사용하는 파일별 현재 최종 메타데이터."""

    __tablename__ = "common_file_metadata"
    __table_args__ = (
        UniqueConstraint(
            "file_id",
            name="uq_common_file_metadata_file_id",
        ),
    )

    id = Column(
        Integer,
        primary_key=True,
        index=True,
    )

    file_id = Column(
        Integer,
        ForeignKey("common_files.id"),
        nullable=False,
        index=True,
    )

    camera_make = Column(
        String(200),
        nullable=True,
    )
    camera_model = Column(
        String(200),
        nullable=True,
    )
    lens = Column(
        String(200),
        nullable=True,
    )
    datetime_original = Column(
        DateTime(timezone=True),
        nullable=True,
    )
    # EXIF wall-clock fact for the capture-date projection.  Keep the legacy
    # timestamptz datetime_original untouched for existing API compatibility.
    original_capture_datetime = Column(
        DateTime(timezone=False),
        nullable=True,
        info=migration_managed_schema_info(),
    )
    gps_lat = Column(
        Float,
        nullable=True,
    )
    gps_lon = Column(
        Float,
        nullable=True,
    )
    gps_alt = Column(
        Float,
        nullable=True,
    )
    iso = Column(
        Integer,
        nullable=True,
    )
    f_number = Column(
        String(50),
        nullable=True,
    )
    exposure_time = Column(
        String(50),
        nullable=True,
    )
    focal_length = Column(
        String(50),
        nullable=True,
    )
    orientation = Column(
        Integer,
        nullable=True,
    )
    image_width = Column(
        Integer,
        nullable=True,
    )
    image_height = Column(
        Integer,
        nullable=True,
    )
    country = Column(
        String(100),
        nullable=True,
    )
    province = Column(
        String(100),
        nullable=True,
    )
    city = Column(
        String(100),
        nullable=True,
    )
    district = Column(
        String(100),
        nullable=True,
    )
    place_name = Column(
        String(200),
        nullable=True,
    )
    memorykeeper_place_id = Column(
        String(36),
        ForeignKey("memorykeeper_places.id"),
        nullable=True,
        index=True,
    )
    place_match_source = Column(String(50), nullable=True)
    place_match_distance_m = Column(Float, nullable=True)
    place_match_revision = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    reserved = Column(
        String(500),
        nullable=True,
    )
    astro_target = Column(
        String(200),
        nullable=True,
    )
    astro_catalog = Column(
        String(100),
        nullable=True,
    )
    astro_ra = Column(
        String(100),
        nullable=True,
    )
    astro_dec = Column(
        String(100),
        nullable=True,
    )
    astro_rotation = Column(
        Float,
        nullable=True,
    )
    astro_fov = Column(
        Float,
        nullable=True,
    )
    astro_object_type = Column(
        String(100),
        nullable=True,
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    locked = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
