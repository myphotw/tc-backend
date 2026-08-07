"""Gallery Query Service."""

from __future__ import annotations

import logging
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_tag import CommonFileTag
from app.common.repositories.gallery_repository import GalleryRepository
from app.common.repositories.tag_repository import TagSource
from app.common.schemas.gallery import (
    CountItem,
    GalleryDetailResponse,
    GalleryListItem,
    GalleryListResponse,
    GallerySearchResponse,
    GalleryTagItem,
    MapMarkerListResponse,
    MapMarkerResponse,
    StatisticsResponse,
    TimelineItem,
    TimelineResponse,
)
from app.common.services.storage_service import StorageService
from app.common.utils.perf import QueryCounter, Stopwatch, log_perf

logger = logging.getLogger(__name__)

MediaKind = Literal["thumbnail", "preview", "original"]


class GalleryService:
    """Gallery 조회 Business Logic."""

    def __init__(self, db: Session) -> None:
        self.repository = GalleryRepository(db)
        self.storage_service = StorageService()

    def get_media(
        self,
        *,
        file_id: str,
        kind: MediaKind,
    ) -> tuple[Path, str]:
        """
        Gallery 이미지 파일 경로와 mime_type을 반환한다.

        Returns:
            tuple[Path, str]: (절대경로, media_type)
        """
        watch = Stopwatch()
        watch.start("db_lookup")
        detail = self.repository.detail(file_id)
        db_lookup_ms = watch.stop("db_lookup")
        if detail is None:
            raise HTTPException(status_code=404, detail="File not found")

        common_file: CommonFile = detail["file"]
        if bool(common_file.deleted):
            logger.warning(
                "Gallery media rejected deleted file_id=%s kind=%s",
                file_id,
                kind,
            )
            raise HTTPException(status_code=404, detail="File not found")

        db_path = self._media_db_path(common_file, kind)
        if not db_path:
            logger.warning(
                "Gallery media path missing file_id=%s kind=%s db_path=%s",
                file_id,
                kind,
                db_path,
            )
            raise HTTPException(status_code=404, detail="Media file not found")

        try:
            watch.start("path_resolve")
            resolved = self.storage_service.resolve_storage_path(db_path)
            path_resolve_ms = watch.stop("path_resolve")
        except Exception as exc:
            logger.exception(
                "Gallery media path resolve failed file_id=%s kind=%s db_path=%s error=%s",
                file_id,
                kind,
                db_path,
                exc,
            )
            raise HTTPException(status_code=404, detail="Media file not found") from exc

        exists = resolved.is_file()
        file_size = resolved.stat().st_size if exists else None
        media_type = self._resolve_media_type(common_file, kind, resolved)
        log_perf(
            "gallery_media",
            kind=kind,
            file_id=file_id,
            db_lookup_ms=db_lookup_ms,
            path_resolve_ms=path_resolve_ms,
            file_size=file_size,
            exists=exists,
            mime_type=media_type,
            cache_control="public, max-age=86400",
            elapsed_ms=watch.total_ms(),
        )
        logger.info(
            "Gallery media file_id=%s kind=%s db_path=%s resolved=%s exists=%s mime_type=%s",
            file_id,
            kind,
            db_path,
            resolved,
            exists,
            media_type,
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Media file not found")
        return resolved, media_type

    @staticmethod
    def _media_db_path(common_file: CommonFile, kind: MediaKind) -> str | None:
        if kind == "thumbnail":
            return common_file.thumb_path
        if kind == "preview":
            return common_file.preview_path
        return common_file.original_path

    @staticmethod
    def _resolve_media_type(
        common_file: CommonFile,
        kind: MediaKind,
        resolved: Path,
    ) -> str:
        if kind == "original" and common_file.mime_type:
            return common_file.mime_type
        guessed, _ = mimetypes.guess_type(resolved.name)
        if guessed:
            return guessed
        if kind in {"thumbnail", "preview"}:
            return "image/jpeg"
        return common_file.mime_type or "application/octet-stream"

    def list_gallery(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "capture_datetime_desc",
        service_name: str | None = "MemoryKeeper",
    ) -> GalleryListResponse:
        return self._timed_list_like(
            endpoint="gallery_list",
            fetch=lambda: self.repository.list(
                page=page,
                page_size=page_size,
                sort=sort,
                service_name=service_name,
            ),
            page=page,
            page_size=page_size,
            sort=sort,
            response_cls=GalleryListResponse,
            service_name=service_name,
        )

    def get_detail(self, file_id: str) -> GalleryDetailResponse:
        watch = Stopwatch()
        bind = self.repository.db.get_bind()
        with QueryCounter(bind) as counter:  # type: ignore[arg-type]
            watch.start("db_query")
            detail = self.repository.detail(file_id)
            db_ms = watch.stop("db_query")
            if detail is None:
                raise HTTPException(status_code=404, detail="File not found")

            watch.start("dto_mapping")
            common_file: CommonFile = detail["file"]
            metadata: CommonFileMetadata | None = detail["metadata"]
            tags: list[CommonFileTag] = detail["tags"]

            ai_tags = [
                self._to_tag_item(tag)
                for tag in tags
                if tag.source == TagSource.AI
            ]
            user_tags = [
                self._to_tag_item(tag)
                for tag in tags
                if tag.source == TagSource.USER
            ]

            response = GalleryDetailResponse(
                file_id=common_file.file_id,
                filename=common_file.original_name,
                extension=common_file.extension,
                mime_type=common_file.mime_type,
                file_size=common_file.file_size,
                width=common_file.width,
                height=common_file.height,
                favorite=bool(common_file.favorite),
                service_name=common_file.service_name or "MemoryKeeper",
                storage_path=common_file.original_path,
                preview_url=self._to_media_url(common_file.file_id, "preview", common_file.preview_path),
                thumbnail_url=self._to_media_url(common_file.file_id, "thumbnail", common_file.thumb_path),
                original_url=self._to_media_url(common_file.file_id, "original", common_file.original_path),
                metadata=self._metadata_dict(metadata),
                ai_tags=ai_tags,
                user_tags=user_tags,
                history_count=int(detail["history_count"]),
                created_at=common_file.created_at,
                updated_at=common_file.updated_at,
            )
            dto_ms = watch.stop("dto_mapping")

        log_perf(
            "gallery_detail",
            db_query_ms=db_ms,
            dto_mapping_ms=dto_ms,
            query_count=counter.count,
            elapsed_ms=watch.total_ms(),
        )
        return response

    def search(
        self,
        *,
        year: int | None = None,
        country: str | None = None,
        city: str | None = None,
        camera: str | None = None,
        tag: str | None = None,
        favorite: bool | None = None,
        service_name: str | None = "MemoryKeeper",
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: str = "capture_datetime_desc",
    ) -> GallerySearchResponse:
        return self._timed_list_like(
            endpoint="gallery_search",
            fetch=lambda: self.repository.search(
                year=year,
                country=country,
                city=city,
                camera=camera,
                tag=tag,
                favorite=favorite,
                service_name=service_name,
                date_from=date_from,
                date_to=date_to,
                keyword=keyword,
                page=page,
                page_size=page_size,
                sort=sort,
            ),
            page=page,
            page_size=page_size,
            sort=sort,
            response_cls=GallerySearchResponse,
            service_name=service_name,
        )

    def map_markers(
        self,
        *,
        service_name: str | None = "MemoryKeeper",
        year: int | None = None,
    ) -> MapMarkerListResponse:
        watch = Stopwatch()
        bind = self.repository.db.get_bind()
        with QueryCounter(bind) as counter:  # type: ignore[arg-type]
            watch.start("db_query")
            rows = self.repository.map(service_name=service_name, year=year)
            db_ms = watch.stop("db_query")
            watch.start("dto_mapping")
            items: list[MapMarkerResponse] = []
            for common_file, metadata in rows:
                if metadata.gps_lat is None or metadata.gps_lon is None:
                    continue
                capture = metadata.datetime_original
                items.append(
                    MapMarkerResponse(
                        file_id=common_file.file_id,
                        latitude=float(metadata.gps_lat),
                        longitude=float(metadata.gps_lon),
                        place_name=metadata.place_name,
                        thumbnail=self._to_media_url(
                            common_file.file_id,
                            "thumbnail",
                            common_file.thumb_path,
                        ),
                        year=capture.year if capture is not None else None,
                        service_name=service_name or common_file.service_name or "MemoryKeeper",
                    )
                )
            response = MapMarkerListResponse(items=items, total=len(items))
            dto_ms = watch.stop("dto_mapping")
        log_perf(
            "gallery_map",
            db_query_ms=db_ms,
            dto_mapping_ms=dto_ms,
            query_count=counter.count,
            item_count=len(items),
            elapsed_ms=watch.total_ms(),
        )
        return response

    def timeline(self, *, service_name: str | None = "MemoryKeeper") -> TimelineResponse:
        watch = Stopwatch()
        bind = self.repository.db.get_bind()
        with QueryCounter(bind) as counter:  # type: ignore[arg-type]
            watch.start("db_query")
            rows = self.repository.timeline(service_name=service_name)
            db_ms = watch.stop("db_query")
            watch.start("dto_mapping")
            items = [TimelineItem(year=year, count=count) for year, count in rows]
            response = TimelineResponse(
                items=items,
                total=sum(item.count for item in items),
            )
            dto_ms = watch.stop("dto_mapping")
        log_perf(
            "gallery_timeline",
            db_query_ms=db_ms,
            dto_mapping_ms=dto_ms,
            query_count=counter.count,
            elapsed_ms=watch.total_ms(),
        )
        return response

    def statistics(self, *, service_name: str | None = "MemoryKeeper") -> StatisticsResponse:
        watch = Stopwatch()
        bind = self.repository.db.get_bind()
        with QueryCounter(bind) as counter:  # type: ignore[arg-type]
            watch.start("db_query")
            stats = self.repository.statistics(service_name=service_name)
            db_ms = watch.stop("db_query")
            watch.start("dto_mapping")
            response = StatisticsResponse(
                total_photos=stats["total_photos"],
                gps_count=stats["gps_count"],
                ai_tag_count=stats["ai_tag_count"],
                by_camera=[CountItem(**item) for item in stats["by_camera"]],
                by_country=[CountItem(**item) for item in stats["by_country"]],
                by_year=[CountItem(**item) for item in stats["by_year"]],
                by_service=[CountItem(**item) for item in stats["by_service"]],
            )
            dto_ms = watch.stop("dto_mapping")
        log_perf(
            "gallery_statistics",
            db_query_ms=db_ms,
            dto_mapping_ms=dto_ms,
            query_count=counter.count,
            elapsed_ms=watch.total_ms(),
        )
        return response

    def _timed_list_like(
        self,
        *,
        endpoint: str,
        fetch,
        page: int,
        page_size: int,
        sort: str,
        response_cls,
        service_name: str | None,
    ):
        watch = Stopwatch()
        bind = self.repository.db.get_bind()
        with QueryCounter(bind) as counter:  # type: ignore[arg-type]
            watch.start("db_query")
            rows, total = fetch()
            db_ms = watch.stop("db_query")
            watch.start("dto_mapping")
            items = [
                self._to_list_item(file, metadata, has_ai_tag, service_name=service_name)
                for file, metadata, has_ai_tag in rows
            ]
            response = response_cls(
                items=items,
                page=page,
                page_size=page_size,
                total=total,
                sort=sort,
            )
            dto_ms = watch.stop("dto_mapping")
        log_perf(
            endpoint,
            db_query_ms=db_ms,
            dto_mapping_ms=dto_ms,
            total_count_query_ms=self.repository.last_section_ms.get(
                "total_count_query_ms"
            ),
            rows_query_ms=self.repository.last_section_ms.get("rows_query_ms"),
            ai_tag_flag_query_ms=self.repository.last_section_ms.get(
                "ai_tag_flag_query_ms"
            ),
            query_count=counter.count,
            item_count=len(items),
            total=total,
            elapsed_ms=watch.total_ms(),
        )
        return response

    def _to_list_item(
        self,
        common_file: CommonFile,
        metadata: CommonFileMetadata | None,
        has_ai_tag: bool,
        *,
        service_name: str | None = None,
    ) -> GalleryListItem:
        return GalleryListItem(
            file_id=common_file.file_id,
            filename=common_file.original_name,
            preview_url=self._to_media_url(
                common_file.file_id,
                "preview",
                common_file.preview_path,
            ),
            thumbnail_url=self._to_media_url(
                common_file.file_id,
                "thumbnail",
                common_file.thumb_path,
            ),
            capture_datetime=metadata.datetime_original if metadata else None,
            country=metadata.country if metadata else None,
            city=metadata.city if metadata else None,
            place_name=metadata.place_name if metadata else None,
            camera_model=metadata.camera_model if metadata else None,
            favorite=bool(common_file.favorite),
            has_gps=bool(
                metadata
                and metadata.gps_lat is not None
                and metadata.gps_lon is not None
            ),
            has_ai_tag=has_ai_tag,
            service_name=service_name or common_file.service_name or "MemoryKeeper",
        )

    @staticmethod
    def _to_tag_item(tag: CommonFileTag) -> GalleryTagItem:
        return GalleryTagItem(
            tag=tag.tag,
            source=tag.source,
            tag_type=tag.tag_type,
            confidence=tag.confidence,
        )

    @staticmethod
    def _to_media_url(
        file_id: str,
        kind: str,
        storage_path: str | None,
    ) -> str | None:
        if not storage_path:
            return None
        return f"/api/common/gallery/{file_id}/{kind}"

    @staticmethod
    def _metadata_dict(metadata: CommonFileMetadata | None) -> dict[str, Any]:
        if metadata is None:
            return {}
        fields = (
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
            "locked",
        )
        result: dict[str, Any] = {}
        for field in fields:
            value = getattr(metadata, field, None)
            if value is not None:
                result[field] = value
        return result
