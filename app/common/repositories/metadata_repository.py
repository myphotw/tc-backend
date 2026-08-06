"""column 기반 common_file_metadata 저장소."""

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

    def upsert_fields(
        self,
        *,
        file_id: int,
        values: dict[str, Any],
        source: str,
        confidence: float | None = None,
        approved: bool = False,
        modified_by: str | None = None,
        commit: bool = True,
    ) -> CommonFileMetadata:
        """
        여러 metadata 필드를 한 트랜잭션에서 upsert한다.

        - metadata row 1회 조회
        - 필드별 최신 priority 1회 조회
        - 변경 필드만 update + history add_all
        - USER locked / source priority 규칙은 기존과 동일
        """
        _ = confidence  # 예약 인자 (향후 confidence 기반 정책용)

        item = self.get_metadata(file_id=file_id)
        if item is None:
            item = CommonFileMetadata(file_id=file_id)
            self.db.add(item)
            self.db.flush()

        priority = self.get_priority(source)
        candidates = {
            field_name: new_value
            for field_name, new_value in values.items()
            if field_name in self.ALLOWED_FIELDS
            and new_value is not None
            and new_value != ""
        }
        if not candidates:
            if commit:
                self.db.commit()
            return item

        if self._is_update_blocked(item=item, source=source):
            if commit:
                self.db.commit()
            return item

        latest_priorities = self.history_repository.get_latest_priorities(
            file_id=file_id,
            field_names=list(candidates.keys()),
        )

        history_items: list[dict[str, Any]] = []
        for field_name, new_value in candidates.items():
            current_priority = latest_priorities.get(field_name, 0)
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

            history_items.append(
                {
                    "file_id": file_id,
                    "field_name": field_name,
                    "old_value": old_value,
                    "new_value": new_value,
                    "source": source,
                    "priority": priority,
                    "modified_by": modified_by,
                    "approved": approved,
                }
            )

        if history_items:
            self.history_repository.create_histories(
                items=history_items,
                commit=False,
            )

        if commit:
            previous = self.db.expire_on_commit
            self.db.expire_on_commit = False
            try:
                self.db.commit()
            finally:
                self.db.expire_on_commit = previous
        else:
            self.db.flush()
        return item

    def save_metadata(
        self,
        *,
        file_id: int,
        metadata: dict[str, Any],
        source: str,
        modified_by: str | None = None,
        commit: bool = True,
    ) -> CommonFileMetadata:
        """
        priority와 locked 규칙에 따라 현재 메타데이터를 저장한다.

        내부적으로 upsert_fields batch API를 사용한다.
        """
        return self.upsert_fields(
            file_id=file_id,
            values=metadata,
            source=source,
            modified_by=modified_by,
            commit=commit,
        )

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
        return self.upsert_fields(
            file_id=file_id,
            values={field_name: value},
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
