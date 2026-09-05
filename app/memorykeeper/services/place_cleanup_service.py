"""User-facing MemoryKeeper place-cleanup list service."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.common.services.gallery_media import build_gallery_media_url
from app.memorykeeper.repositories.place_cleanup_repository import (
    MemoryKeeperPlaceCleanupRepository,
)
from app.memorykeeper.schemas.pending import PendingFileItem, PendingListResponse


class MemoryKeeperPlaceCleanupService:
    def __init__(self, db: Session) -> None:
        self.repository = MemoryKeeperPlaceCleanupRepository(db)

    def list(
        self,
        *,
        page: int,
        page_size: int,
    ) -> PendingListResponse:
        rows, total = self.repository.list(page=page, page_size=page_size)
        items: list[PendingFileItem] = []
        for common_file, metadata in rows:
            items.append(
                PendingFileItem(
                    file_id=common_file.file_id,
                    thumbnail_url=build_gallery_media_url(
                        common_file.file_id,
                        "thumbnail",
                        common_file.thumb_path,
                    ),
                    capture_datetime=(
                        metadata.datetime_original if metadata else None
                    ),
                    gps_lat=metadata.gps_lat if metadata else None,
                    gps_lon=metadata.gps_lon if metadata else None,
                    country=metadata.country if metadata else None,
                    province=metadata.province if metadata else None,
                    city=metadata.city if metadata else None,
                    district=metadata.district if metadata else None,
                    place_name=metadata.place_name if metadata else None,
                    memorykeeper_place_id=(
                        metadata.memorykeeper_place_id if metadata else None
                    ),
                    place_revision=(
                        int(metadata.place_match_revision or 0) if metadata else 0
                    ),
                )
            )
        return PendingListResponse(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
        )
