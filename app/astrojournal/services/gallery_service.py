from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.astrojournal.repositories.gallery_repository import (
    AstroGalleryRepository,
    AstroGalleryRow,
)
from app.astrojournal.schemas.gallery import (
    AstroGalleryDetailItem,
    AstroGalleryItem,
    AstroGalleryListResponse,
)
from app.astrojournal.services.plate_solve_read_service import PlateSolveReadService
from app.common.services.gallery_media import build_gallery_media_url


class AstroGalleryService:
    def __init__(self, db: Session) -> None:
        self.repository = AstroGalleryRepository(db)

    def list_gallery(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        catalog_object_id: str | None = None,
        favorite: bool | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> AstroGalleryListResponse:
        rows, total = self.repository.list(
            page=page,
            page_size=page_size,
            catalog_object_id=catalog_object_id,
            favorite=favorite,
            date_from=date_from,
            date_to=date_to,
        )
        return AstroGalleryListResponse(
            items=[self._to_item(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )

    def get_detail(self, record_id: str) -> AstroGalleryDetailItem:
        row = self.repository.get(record_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
        return self._to_item(row, detail=True)

    @staticmethod
    def _to_item(
        row: AstroGalleryRow,
        *,
        detail: bool = False,
    ) -> AstroGalleryItem | AstroGalleryDetailItem:
        record, common_file, metadata, plate_solve_job = row
        projection = PlateSolveReadService.from_job(
            plate_solve_job,
            fallback_status=record.plate_solve_status,
            include_result=detail,
        )
        item_data = dict(
            record_id=record.id,
            revision=record.revision,
            catalog_object_id=record.catalog_object_id,
            captured_at=record.captured_at,
            latitude=record.latitude,
            longitude=record.longitude,
            location_name=record.location_name,
            memo=record.memo,
            favorite=bool(record.favorite),
            representative=bool(record.representative),
            file_id=common_file.file_id,
            common_file_id=common_file.id,
            filename=common_file.original_name,
            mime_type=common_file.mime_type,
            thumbnail_url=build_gallery_media_url(
                common_file.file_id,
                "thumbnail",
                common_file.thumb_path,
            ),
            preview_url=build_gallery_media_url(
                common_file.file_id,
                "preview",
                common_file.preview_path,
            ),
            original_url=build_gallery_media_url(
                common_file.file_id,
                "original",
                common_file.original_path,
            ),
            capture_datetime=metadata.datetime_original if metadata is not None else None,
            plate_solve_status=projection.plate_solve_status,
            plate_solve_job_id=projection.plate_solve_job_id,
        )
        if detail:
            return AstroGalleryDetailItem(
                **item_data,
                plate_solve_result=projection.plate_solve_result,
            )
        return AstroGalleryItem(**item_data)
