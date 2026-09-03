"""MemoryKeeper-only fast-read Gallery service."""

from __future__ import annotations

from datetime import date

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.common.services.gallery_media import build_gallery_media_url
from app.memorykeeper.repositories.fast_gallery_repository import (
    FastGalleryFilters,
    MemoryKeeperFastGalleryRepository,
)
from app.memorykeeper.schemas.fast_gallery import (
    FastGalleryCountryNode,
    FastGalleryCount,
    FastGalleryHierarchyResponse,
    FastGalleryPhotoItem,
    FastGalleryPhotosResponse,
    FastGalleryPlaceNode,
    FastGalleryRegionNode,
    FastGallerySummaryResponse,
    FastGalleryYearNode,
)
from app.memorykeeper.services.fast_gallery_cursor import (
    FastGalleryCursor,
    decode_cursor,
    encode_cursor,
)


class MemoryKeeperFastGalleryService:
    """Serve card reads and aggregates without materializing a photo catalog."""

    def __init__(self, db: Session) -> None:
        self.repository = MemoryKeeperFastGalleryRepository(db)

    def photos(
        self,
        *,
        cursor: str | None,
        limit: int,
        filters: FastGalleryFilters,
    ) -> FastGalleryPhotosResponse:
        self._validate_dates(filters)
        decoded = decode_cursor(cursor) if cursor else None
        rows = self.repository.photos(
            filters=filters,
            limit=limit,
            cursor_datetime=(decoded.effective_capture_datetime if decoded else None),
            cursor_file_id=(decoded.file_id if decoded else None),
        )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        items = [self._to_photo_item(row) for row in page_rows]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = encode_cursor(
                FastGalleryCursor(
                    effective_capture_datetime=last.effective_capture_datetime,
                    file_id=last.common_file_id,
                )
            )
        return FastGalleryPhotosResponse(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
            # See schema comment: a full upload CREATE delta contract does not
            # exist yet, so do not claim one through this endpoint.
            sync_cursor=None,
        )

    def summary(self) -> FastGallerySummaryResponse:
        payload = self.repository.summary()
        return FastGallerySummaryResponse(
            total_photos=payload["total_photos"],  # type: ignore[arg-type]
            favorite_count=payload["favorite_count"],  # type: ignore[arg-type]
            gps_count=payload["gps_count"],  # type: ignore[arg-type]
            effective_date_min=payload["effective_date_min"],  # type: ignore[arg-type]
            effective_date_max=payload["effective_date_max"],  # type: ignore[arg-type]
            by_year=[
                FastGalleryCount(name=str(year), count=count)
                for year, count in payload["by_year"]  # type: ignore[index]
            ],
            by_country=[
                FastGalleryCount(name=name, count=count)
                for name, count in payload["by_country"]  # type: ignore[index]
            ],
        )

    def hierarchy(self) -> FastGalleryHierarchyResponse:
        years: dict[int, dict[object, object]] = {}
        for row in self.repository.hierarchy():
            year = int(row.year)
            country = row.country
            region = row.region
            place_id = row.memorykeeper_place_id
            count = int(row.count)
            year_bucket = years.setdefault(
                year,
                {"count": 0, "countries": {}},
            )
            year_bucket["count"] += count  # type: ignore[index]
            countries = year_bucket["countries"]  # type: ignore[index]
            country_bucket = countries.setdefault(  # type: ignore[union-attr]
                country,
                {"count": 0, "regions": {}},
            )
            country_bucket["count"] += count  # type: ignore[index]
            regions = country_bucket["regions"]  # type: ignore[index]
            region_bucket = regions.setdefault(  # type: ignore[union-attr]
                region,
                {"count": 0, "places": []},
            )
            region_bucket["count"] += count  # type: ignore[index]
            region_bucket["places"].append(  # type: ignore[index]
                FastGalleryPlaceNode(
                    memorykeeper_place_id=place_id,
                    display_name=row.place_display_name,
                    count=count,
                )
            )

        return FastGalleryHierarchyResponse(
            items=[
                FastGalleryYearNode(
                    year=year,
                    count=year_bucket["count"],  # type: ignore[index]
                    countries=[
                        FastGalleryCountryNode(
                            country=country,
                            count=country_bucket["count"],  # type: ignore[index]
                            regions=[
                                FastGalleryRegionNode(
                                    region=region,
                                    count=region_bucket["count"],  # type: ignore[index]
                                    places=region_bucket["places"],  # type: ignore[index]
                                )
                                for region, region_bucket in country_bucket["regions"].items()  # type: ignore[index]
                            ],
                        )
                        for country, country_bucket in year_bucket["countries"].items()  # type: ignore[index]
                    ],
                )
                for year, year_bucket in years.items()
            ]
        )

    @staticmethod
    def _to_photo_item(row: object) -> FastGalleryPhotoItem:
        return FastGalleryPhotoItem(
            common_file_id=int(row.common_file_id),  # type: ignore[attr-defined]
            file_id=str(row.file_id),  # type: ignore[attr-defined]
            filename=str(row.filename),  # type: ignore[attr-defined]
            extension=row.extension,  # type: ignore[attr-defined]
            mime_type=row.mime_type,  # type: ignore[attr-defined]
            preview_url=build_gallery_media_url(
                str(row.file_id),  # type: ignore[attr-defined]
                "preview",
                row.preview_path,  # type: ignore[attr-defined]
            ),
            thumbnail_url=build_gallery_media_url(
                str(row.file_id),  # type: ignore[attr-defined]
                "thumbnail",
                row.thumb_path,  # type: ignore[attr-defined]
            ),
            favorite=bool(row.favorite),  # type: ignore[attr-defined]
            has_gps=bool(row.has_gps),  # type: ignore[attr-defined]
            effective_capture_datetime=row.effective_capture_datetime,  # type: ignore[attr-defined]
            effective_capture_date=row.effective_capture_date,  # type: ignore[attr-defined]
            effective_capture_year=int(row.effective_capture_year),  # type: ignore[attr-defined]
            date_basis=row.date_basis,  # type: ignore[attr-defined]
            memorykeeper_place_id=row.memorykeeper_place_id,  # type: ignore[attr-defined]
            place_display_name=row.place_display_name,  # type: ignore[attr-defined]
            country=row.country,  # type: ignore[attr-defined]
            region=row.region,  # type: ignore[attr-defined]
        )

    @staticmethod
    def _validate_dates(filters: FastGalleryFilters) -> None:
        if filters.date_from is not None and filters.date_to is not None:
            if filters.date_from > filters.date_to:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "INVALID_GALLERY_DATE_RANGE",
                        "message": "date_from must be on or before date_to",
                    },
                )
