from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.place import MemoryKeeperPlace


class MemoryKeeperPlaceRepository:
    SERVICE_NAME = "MemoryKeeper"

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, place_id: str, *, include_deleted: bool = False) -> MemoryKeeperPlace | None:
        query = self.db.query(MemoryKeeperPlace).filter(MemoryKeeperPlace.id == place_id)
        if not include_deleted:
            query = query.filter(MemoryKeeperPlace.deleted_at.is_(None))
        return query.first()

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

    def has_memorykeeper_link(self, file_id: int) -> bool:
        return (
            self.db.query(CommonFileService.id)
            .filter(CommonFileService.file_id == file_id)
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .first()
            is not None
        )
