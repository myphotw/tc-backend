from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.repositories.place_cleanup_repository import (
    memorykeeper_country_expression,
    memorykeeper_place_display_expression,
    memorykeeper_region_expression,
)


class MemoryKeeperPlaceRepository:
    SERVICE_NAME = "MemoryKeeper"

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(
        self,
        place_id: str,
        *,
        include_deleted: bool = False,
        lock: bool = False,
    ) -> MemoryKeeperPlace | None:
        query = self.db.query(MemoryKeeperPlace).filter(MemoryKeeperPlace.id == place_id)
        if not include_deleted:
            query = query.filter(MemoryKeeperPlace.deleted_at.is_(None))
        if lock:
            query = query.with_for_update()
        return query.first()

    def get_many_for_update(self, place_ids: list[str]) -> list[MemoryKeeperPlace]:
        """Lock Place rows in a deterministic order for a cross-Place mutation."""
        return (
            self.db.query(MemoryKeeperPlace)
            .filter(MemoryKeeperPlace.id.in_(place_ids))
            .order_by(MemoryKeeperPlace.id.asc())
            .with_for_update()
            .all()
        )

    def list(
        self,
        *,
        active: bool | None = None,
        favorite: bool | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[MemoryKeeperPlace], int]:
        rows = self.db.query(MemoryKeeperPlace).filter(MemoryKeeperPlace.deleted_at.is_(None))
        if active is not None:
            rows = rows.filter(MemoryKeeperPlace.active.is_(active))
        if favorite is not None:
            rows = rows.filter(MemoryKeeperPlace.favorite.is_(favorite))
        if query:
            like = f"%{query.strip()}%"
            rows = rows.filter(
                or_(
                    MemoryKeeperPlace.display_name.ilike(like),
                    MemoryKeeperPlace.canonical_name.ilike(like),
                    MemoryKeeperPlace.address.ilike(like),
                    MemoryKeeperPlace.city.ilike(like),
                    MemoryKeeperPlace.district.ilike(like),
                )
            )
        total = rows.count()
        items = (
            rows.order_by(
                MemoryKeeperPlace.favorite.desc(),
                MemoryKeeperPlace.usage_count.desc(),
                MemoryKeeperPlace.last_used_at.desc().nullslast(),
                MemoryKeeperPlace.display_name.asc(),
                MemoryKeeperPlace.id.asc(),
            )
            .offset(offset)
            .limit(limit)
            .all()
        )
        return items, total

    def active_places(self) -> list[MemoryKeeperPlace]:
        return (
            self.db.query(MemoryKeeperPlace)
            .filter(MemoryKeeperPlace.deleted_at.is_(None))
            .filter(MemoryKeeperPlace.active.is_(True))
            .order_by(MemoryKeeperPlace.id.asc())
            .all()
        )

    def active_places_bounded(
        self,
        *,
        limit: int,
    ) -> tuple[list[MemoryKeeperPlace], int]:
        query = (
            self.db.query(MemoryKeeperPlace)
            .filter(MemoryKeeperPlace.deleted_at.is_(None))
            .filter(MemoryKeeperPlace.active.is_(True))
        )
        total = query.count()
        return query.order_by(MemoryKeeperPlace.id.asc()).limit(limit).all(), total

    def linked_photo_counts(self, place_ids: list[str]) -> dict[str, int]:
        """Count active MemoryKeeper photos actually assigned to each Place UUID."""
        if not place_ids:
            return {}
        rows = (
            self.db.query(
                CommonFileMetadata.memorykeeper_place_id,
                func.count(func.distinct(CommonFile.id)),
            )
            .join(CommonFile, CommonFile.id == CommonFileMetadata.file_id)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .filter(CommonFileMetadata.memorykeeper_place_id.in_(place_ids))
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .group_by(CommonFileMetadata.memorykeeper_place_id)
            .all()
        )
        return {str(place_id): int(count) for place_id, count in rows}

    def linked_photo_count(self, place_id: str) -> int:
        return self.linked_photo_counts([place_id]).get(place_id, 0)

    def total_reference_count(self, place_id: str) -> int:
        """Count every FK reference, including deleted or no-longer-linked files."""
        return int(
            self.db.query(func.count(CommonFileMetadata.file_id))
            .filter(CommonFileMetadata.memorykeeper_place_id == place_id)
            .scalar()
            or 0
        )

    def relation_rows_for_update(
        self,
        place_ids: list[str],
    ) -> list[tuple[CommonFileMetadata, CommonFile]]:
        return (
            self.db.query(CommonFileMetadata, CommonFile)
            .join(CommonFile, CommonFile.id == CommonFileMetadata.file_id)
            .filter(CommonFileMetadata.memorykeeper_place_id.in_(place_ids))
            .order_by(CommonFileMetadata.file_id.asc())
            .with_for_update()
            .all()
        )

    def linked_photos(
        self,
        place_id: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[tuple[CommonFile, CommonFileMetadata, MemoryKeeperFileState | None]], int]:
        """Return the exact active MemoryKeeper relation set, not a radius match."""
        query = (
            self.db.query(CommonFile, CommonFileMetadata, MemoryKeeperFileState)
            .join(CommonFileMetadata, CommonFileMetadata.file_id == CommonFile.id)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .outerjoin(
                MemoryKeeperFileState,
                MemoryKeeperFileState.file_id == CommonFile.id,
            )
            .filter(CommonFileMetadata.memorykeeper_place_id == place_id)
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
        )
        total = query.count()
        rows = (
            query.order_by(
                MemoryKeeperFileState.effective_capture_year.desc().nullslast(),
                MemoryKeeperFileState.effective_capture_datetime.desc().nullslast(),
                CommonFile.id.desc(),
            )
            .offset(offset)
            .limit(limit)
            .all()
        )
        return rows, total

    def hierarchy_rows(self) -> list[object]:
        """Return selectable registered Places grouped by Gallery canonical path."""
        linked_metadata = (
            self.db.query(
                CommonFile.id.label("file_id"),
                CommonFileMetadata.memorykeeper_place_id.label("place_id"),
                CommonFileMetadata.country.label("country"),
                CommonFileMetadata.province.label("province"),
                CommonFileMetadata.city.label("city"),
                CommonFileMetadata.place_name.label("place_name"),
            )
            .join(CommonFileMetadata, CommonFileMetadata.file_id == CommonFile.id)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .subquery("memorykeeper_place_hierarchy_files")
        )
        country = memorykeeper_country_expression(
            place=MemoryKeeperPlace,
            metadata=linked_metadata.c,
        )
        region = memorykeeper_region_expression(
            place=MemoryKeeperPlace,
            metadata=linked_metadata.c,
        )
        display_name = memorykeeper_place_display_expression(
            place=MemoryKeeperPlace,
            metadata=linked_metadata.c,
        )
        return (
            self.db.query(
                MemoryKeeperPlace.id.label("memorykeeper_place_id"),
                country.label("country"),
                region.label("region"),
                display_name.label("display_name"),
                func.count(linked_metadata.c.file_id).label("photo_count"),
            )
            .outerjoin(
                linked_metadata,
                linked_metadata.c.place_id == MemoryKeeperPlace.id,
            )
            .filter(MemoryKeeperPlace.deleted_at.is_(None))
            .filter(MemoryKeeperPlace.active.is_(True))
            .group_by(
                MemoryKeeperPlace.id,
                country,
                region,
                display_name,
            )
            .order_by(
                country.asc().nulls_last(),
                region.asc().nulls_last(),
                display_name.asc().nulls_last(),
                MemoryKeeperPlace.id.asc(),
            )
            .all()
        )

    def get_by_auto_dedup_key(self, key: str) -> MemoryKeeperPlace | None:
        return (
            self.db.query(MemoryKeeperPlace)
            .filter(MemoryKeeperPlace.auto_dedup_key == key)
            .filter(MemoryKeeperPlace.deleted_at.is_(None))
            .first()
        )

    def create(self, place: MemoryKeeperPlace) -> MemoryKeeperPlace:
        self.db.add(place)
        self.db.flush()
        return place

    def update_if_revision(
        self,
        place_id: str,
        *,
        revision: int,
        values: dict[str, object],
    ) -> MemoryKeeperPlace | None:
        count = (
            self.db.query(MemoryKeeperPlace)
            .filter(MemoryKeeperPlace.id == place_id)
            .filter(MemoryKeeperPlace.deleted_at.is_(None))
            .filter(MemoryKeeperPlace.revision == revision)
            .update(
                {
                    **values,
                    MemoryKeeperPlace.revision: revision + 1,
                    MemoryKeeperPlace.updated_at: datetime.now(timezone.utc),
                },
                synchronize_session=False,
            )
        )
        if not count:
            return None
        self.db.flush()
        return self.get(place_id)

    def touch_usage(self, place: MemoryKeeperPlace) -> None:
        place.usage_count = int(place.usage_count or 0) + 1
        place.last_used_at = datetime.now(timezone.utc)

    def memorykeeper_files_with_gps(
        self,
    ) -> list[tuple[CommonFile, CommonFileMetadata]]:
        return (
            self.db.query(CommonFile, CommonFileMetadata)
            .join(CommonFileMetadata, CommonFileMetadata.file_id == CommonFile.id)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .filter(CommonFileMetadata.gps_lat.isnot(None))
            .filter(CommonFileMetadata.gps_lon.isnot(None))
            .order_by(CommonFile.id.asc())
            .all()
        )

    def memorykeeper_files_with_gps_and_state(
        self,
    ) -> list[tuple[CommonFile, CommonFileMetadata, MemoryKeeperFileState | None]]:
        return (
            self.db.query(CommonFile, CommonFileMetadata, MemoryKeeperFileState)
            .join(CommonFileMetadata, CommonFileMetadata.file_id == CommonFile.id)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .outerjoin(
                MemoryKeeperFileState,
                MemoryKeeperFileState.file_id == CommonFile.id,
            )
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .filter(CommonFileMetadata.gps_lat.isnot(None))
            .filter(CommonFileMetadata.gps_lon.isnot(None))
            .order_by(CommonFile.id.asc())
            .all()
        )

    def has_memorykeeper_link(self, file_id: int) -> bool:
        return (
            self.db.query(CommonFileService.id)
            .filter(CommonFileService.file_id == file_id)
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .first()
            is not None
        )
