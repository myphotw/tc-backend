from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.common.models.file_metadata import CommonFileMetadata
from app.common.repositories.history_repository import HistoryRepository
from app.common.repositories.metadata_priority import MetadataPriority


class MetadataSource:
    """메타데이터 출처 값."""

    SYSTEM = "SYSTEM"
    EXIF = "EXIF"
    GPS = "GPS"
    VISION = "VISION"
    PLATESOLVE = "PLATESOLVE"
    USER = "USER"


class MetadataRepository:
    """column 기반 common_file_metadata 저장소."""

    ALLOWED_FIELDS: set[str] = {
        "camera_make",
        "camera_model",
        "lens",
        "datetime_original",
        "gps_lat",
        "gps_lon",
        "gps_alt",
        "iso",
        "f_number",
        "exposure_time",
        "focal_length",
        "orientation",
        "image_width",
        "image_height",
        "country",
        "province",
        "city",
        "district",
        "place_name",
        "reserved",
        "astro_target",
        "astro_catalog",
        "astro_ra",
        "astro_dec",
        "astro_rotation",
        "astro_fov",
        "astro_object_type",
    }

    def __init__(self, db: Session) -> None:
        self.db = db
        self.history_repository = HistoryRepository(db)

    def save_metadata(
        self,
        *,
        file_id: int,
        metadata: dict[str, Any],
        source: str,
        modified_by: str | None = None,
    ) -> CommonFileMetadata:
        """
        priority와 locked 규칙에 따라 현재 메타데이터를 저장한다.

        업데이트가 발생한 필드는 반드시 common_metadata_history에 기록한다.
        """
        item = self.get_metadata(file_id=file_id)
        if item is None:
            item = CommonFileMetadata(file_id=file_id)
            self.db.add(item)
            self.db.flush()

        priority = self.get_priority(source)
        for field_name, new_value in metadata.items():
            if field_name not in self.ALLOWED_FIELDS:
                continue
            if new_value is None or new_value == "":
                continue
            if self._is_update_blocked(item=item, source=source):
                continue

            current_priority = self.history_repository.get_latest_priority(
                file_id=file_id,
                field_name=field_name,
            )
            if priority < current_priority:
                continue
            if priority == current_priority and source != MetadataSource.USER:
                continue

            old_value = getattr(item, field_name)
            if old_value == new_value:
                continue

            setattr(item, field_name, new_value)
            if source == MetadataSource.USER:
                item.locked = True

            self.history_repository.create_history(
                file_id=file_id,
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
                source=source,
                priority=priority,
                modified_by=modified_by,
                commit=False,
            )

        self.db.commit()
        self.db.refresh(item)
        return item

    def get_metadata(self, *, file_id: int) -> CommonFileMetadata | None:
        """파일의 현재 메타데이터 Row를 조회한다."""
        return (
            self.db.query(CommonFileMetadata)
            .filter(CommonFileMetadata.file_id == file_id)
            .first()
        )

    def update_metadata(
        self,
        *,
        file_id: int,
        field_name: str,
        value: Any,
        source: str,
        modified_by: str | None = None,
    ) -> CommonFileMetadata:
        """단일 필드 메타데이터를 저장 규칙에 따라 수정한다."""
        return self.save_metadata(
            file_id=file_id,
            metadata={field_name: value},
            source=source,
            modified_by=modified_by,
        )

    def delete_metadata(self, *, file_id: int) -> bool:
        """파일의 현재 메타데이터 Row를 삭제한다."""
        item = self.get_metadata(file_id=file_id)
        if item is None:
            return False
        self.db.delete(item)
        self.db.commit()
        return True

    def get_priority(self, source: str) -> MetadataPriority:
        """source의 priority 값을 반환한다."""
        return MetadataPriority.from_source(source)

    def _is_update_blocked(self, *, item: CommonFileMetadata, source: str) -> bool:
        """locked Row에 대한 비사용자 source 업데이트를 차단한다."""
        return bool(item.locked and source != MetadataSource.USER)
