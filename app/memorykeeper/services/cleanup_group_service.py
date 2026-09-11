"""Application mapping for authoritative MemoryKeeper cleanup groups."""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.common.services.gallery_media import build_gallery_media_url
from app.memorykeeper.repositories.cleanup_group_repository import (
    MemoryKeeperCleanupGroupRepository,
)
from app.memorykeeper.schemas.cleanup import (
    CaptureDateCleanupGroup,
    CaptureDateCleanupGroupPage,
    CaptureDateCleanupPhoto,
    CaptureDateCleanupPhotoPage,
    PlaceCleanupGroup,
    PlaceCleanupGroupPage,
    PlaceCleanupPhoto,
    PlaceCleanupPhotoPage,
)
from app.memorykeeper.services.cleanup_cursor import (
    decode_group_id,
    decode_page_cursor,
    encode_group_id,
    encode_page_cursor,
)


class MemoryKeeperCleanupGroupService:
    PLACE_QUEUE = "place"
    DATE_QUEUE = "capture_date"

    def __init__(self, db: Session) -> None:
        self.repository = MemoryKeeperCleanupGroupRepository(db)

    def place_groups(self, *, limit: int, cursor: str | None) -> PlaceCleanupGroupPage:
        rows, total_groups, total_photos = self.repository.place_groups(
            limit=limit,
            cursor=(
                decode_page_cursor(cursor, queue=self.PLACE_QUEUE, level="group")
                if cursor
                else None
            ),
        )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items: list[PlaceCleanupGroup] = []
        for row in page_rows:
            key = {
                "issue_type": row.issue_type,
                "capture_date": row.capture_date.isoformat() if row.capture_date else None,
                "gps_lat": row.gps_lat,
                "gps_lon": row.gps_lon,
                "country": row.country,
                "province": row.province,
                "city": row.city,
                "district": row.district,
                "place_name": row.place_name,
            }
            estimated = next(
                (
                    value
                    for value in (row.place_name, row.district, row.city, row.province, row.country)
                    if value and str(value).strip()
                ),
                None,
            )
            items.append(
                PlaceCleanupGroup(
                    group_id=encode_group_id(self.PLACE_QUEUE, key),
                    issue_type=row.issue_type,
                    title=(estimated or "장소 미분류"),
                    media_count=int(row.media_count),
                    first_effective_capture_datetime=row.first_capture,
                    last_effective_capture_datetime=row.last_capture,
                    estimated_location=estimated,
                    representative_file_id=row.representative_file_id,
                    representative_thumbnail_url=build_gallery_media_url(
                        row.representative_file_id,
                        "thumbnail",
                        row.representative_thumb_path,
                    ),
                )
            )
        return PlaceCleanupGroupPage(
            items=items,
            next_cursor=self._next_cursor(
                page_rows,
                has_more,
                queue=self.PLACE_QUEUE,
                level="group",
            ),
            has_more=has_more,
            total_groups=total_groups,
            total_photos=total_photos,
        )

    def place_group_photos(
        self,
        *,
        group_id: str,
        limit: int,
        cursor: str | None,
    ) -> PlaceCleanupPhotoPage:
        key = decode_group_id(group_id, self.PLACE_QUEUE)
        rows, total = self.repository.place_group_photos(
            key=key,
            limit=limit,
            cursor=(
                decode_page_cursor(cursor, queue=self.PLACE_QUEUE, level="photo")
                if cursor
                else None
            ),
        )
        if total == 0:
            self._group_not_found()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        return PlaceCleanupPhotoPage(
            items=[
                PlaceCleanupPhoto(
                    file_id=row.file_id,
                    thumbnail_url=build_gallery_media_url(row.file_id, "thumbnail", row.thumb_path),
                    effective_capture_datetime=row.effective_capture_datetime,
                    gps_lat=row.gps_lat,
                    gps_lon=row.gps_lon,
                    country=row.country,
                    province=row.province,
                    city=row.city,
                    district=row.district,
                    place_name=row.place_name,
                    memorykeeper_place_id=row.memorykeeper_place_id,
                    place_revision=int(row.place_match_revision or 0),
                )
                for row in page_rows
            ],
            next_cursor=self._next_cursor(
                page_rows,
                has_more,
                queue=self.PLACE_QUEUE,
                level="photo",
                id_name="common_file_id",
            ),
            has_more=has_more,
            total_photos=total,
        )

    def date_groups(self, *, limit: int, cursor: str | None) -> CaptureDateCleanupGroupPage:
        rows, total_groups, total_photos = self.repository.date_groups(
            limit=limit,
            cursor=(
                decode_page_cursor(cursor, queue=self.DATE_QUEUE, level="group")
                if cursor
                else None
            ),
        )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [
            CaptureDateCleanupGroup(
                group_id=encode_group_id(
                    self.DATE_QUEUE,
                    {
                        "cleanup_reason": row.cleanup_reason,
                        "date_basis": row.date_basis,
                        "capture_date": row.capture_date.isoformat() if row.capture_date else None,
                    },
                ),
                title=(
                    row.capture_date.isoformat()
                    if row.capture_date is not None
                    else "촬영일 없음"
                ),
                media_count=int(row.media_count),
                first_effective_capture_datetime=row.first_capture,
                last_effective_capture_datetime=row.last_capture,
                cleanup_reason=row.cleanup_reason,
                date_basis=row.date_basis,
                representative_file_id=row.representative_file_id,
                representative_thumbnail_url=build_gallery_media_url(
                    row.representative_file_id,
                    "thumbnail",
                    row.representative_thumb_path,
                ),
            )
            for row in page_rows
        ]
        return CaptureDateCleanupGroupPage(
            items=items,
            next_cursor=self._next_cursor(
                page_rows,
                has_more,
                queue=self.DATE_QUEUE,
                level="group",
            ),
            has_more=has_more,
            total_groups=total_groups,
            total_photos=total_photos,
        )

    def date_group_photos(
        self,
        *,
        group_id: str,
        limit: int,
        cursor: str | None,
    ) -> CaptureDateCleanupPhotoPage:
        key = decode_group_id(group_id, self.DATE_QUEUE)
        rows, total = self.repository.date_group_photos(
            key=key,
            limit=limit,
            cursor=(
                decode_page_cursor(cursor, queue=self.DATE_QUEUE, level="photo")
                if cursor
                else None
            ),
        )
        if total == 0:
            self._group_not_found()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        return CaptureDateCleanupPhotoPage(
            items=[
                CaptureDateCleanupPhoto(
                    file_id=row.file_id,
                    raw_capture_datetime=row.original_capture_datetime,
                    effective_capture_datetime=row.effective_capture_datetime,
                    effective_capture_date=row.effective_capture_date,
                    effective_capture_year=row.effective_capture_year,
                    date_basis=row.date_basis,
                    date_cleanup_required=True,
                    date_cleanup_reason=row.cleanup_reason,
                    user_capture_datetime=row.user_capture_datetime,
                    user_capture_precision=row.user_capture_precision,
                    date_revision=int(row.date_revision or 0),
                    thumbnail_url=build_gallery_media_url(row.file_id, "thumbnail", row.thumb_path),
                )
                for row in page_rows
            ],
            next_cursor=self._next_cursor(
                page_rows,
                has_more,
                queue=self.DATE_QUEUE,
                level="photo",
                id_name="common_file_id",
            ),
            has_more=has_more,
            total_photos=total,
        )

    @staticmethod
    def _next_cursor(
        rows: list[object],
        has_more: bool,
        *,
        queue: str,
        level: str,
        id_name: str = "tie_breaker",
    ) -> str | None:
        if not has_more or not rows:
            return None
        last = rows[-1]
        captured_at = getattr(last, "last_capture", None)
        if id_name == "common_file_id":
            captured_at = getattr(last, "effective_capture_datetime")
        return encode_page_cursor(
            captured_at,
            int(getattr(last, id_name)),
            queue=queue,
            level=level,
        )

    @staticmethod
    def _group_not_found() -> None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CLEANUP_GROUP_NOT_FOUND", "message": "Cleanup group not found"},
        )
