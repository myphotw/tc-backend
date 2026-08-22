"""Gallery Query Repository."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Query, Session

from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_tag import CommonFileTag
from app.common.models.metadata_history import CommonMetadataHistory
from app.common.repositories.tag_repository import TagSource
from app.common.utils.perf import Stopwatch, log_perf
from app.memorykeeper.models.file_state import MemoryKeeperFileState


class GalleryRepository:
    """common_files / metadata / tags 조회 저장소."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.last_section_ms: dict[str, float] = {}

    def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str = "capture_datetime_desc",
        service_name: str | None = None,
        incomplete: bool | None = None,
    ) -> tuple[list[tuple[CommonFile, CommonFileMetadata | None, bool]], int]:
        """사진 목록을 조회한다."""
        watch = Stopwatch()
        query = self._base_query(service_name=service_name)
        query = self._apply_incomplete(
            query,
            incomplete=incomplete,
            service_name=service_name,
        )
        watch.start("total_count_query")
        total = query.count()
        count_ms = watch.stop("total_count_query")
        query = self._apply_sort(query, sort)
        watch.start("rows_query")
        rows = query.offset(max(page - 1, 0) * page_size).limit(page_size).all()
        rows_ms = watch.stop("rows_query")
        watch.start("ai_tag_flag_query")
        result = self._with_ai_tag_flags(rows)
        ai_ms = watch.stop("ai_tag_flag_query")
        self.last_section_ms = {
            "total_count_query_ms": count_ms,
            "rows_query_ms": rows_ms,
            "ai_tag_flag_query_ms": ai_ms,
        }
        log_perf(
            "gallery_repository_list",
            total_count_query_ms=count_ms,
            rows_query_ms=rows_ms,
            ai_tag_flag_query_ms=ai_ms,
            row_count=len(rows),
            total=total,
            elapsed_ms=watch.total_ms(),
        )
        return result, total

    def detail(self, file_id: str) -> dict[str, Any] | None:
        """사진 상세를 조회한다."""
        row = (
            self._base_query()
            .filter(CommonFile.file_id == file_id)
            .first()
        )
        if row is None:
            return None

        common_file, metadata = row
        tags = (
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.file_id == common_file.id)
            .filter(CommonFileTag.deleted.is_(False))
            .order_by(CommonFileTag.created_at.desc())
            .all()
        )
        history_count = (
            self.db.query(func.count(CommonMetadataHistory.id))
            .filter(CommonMetadataHistory.file_id == common_file.id)
            .scalar()
            or 0
        )
        return {
            "file": common_file,
            "metadata": metadata,
            "tags": tags,
            "history_count": int(history_count),
        }

    def search(
        self,
        *,
        year: int | None = None,
        country: str | None = None,
        city: str | None = None,
        camera: str | None = None,
        tag: str | None = None,
        favorite: bool | None = None,
        service_name: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        keyword: str | None = None,
        incomplete: bool | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: str = "capture_datetime_desc",
    ) -> tuple[list[tuple[CommonFile, CommonFileMetadata | None, bool]], int]:
        """복수 조건 검색을 수행한다."""
        watch = Stopwatch()
        query = self._base_query(service_name=service_name)
        query = self._apply_filters(
            query,
            year=year,
            country=country,
            city=city,
            camera=camera,
            tag=tag,
            favorite=favorite,
            date_from=date_from,
            date_to=date_to,
            keyword=keyword,
            incomplete=incomplete,
            service_name=service_name,
        )
        watch.start("total_count_query")
        total = query.count()
        count_ms = watch.stop("total_count_query")
        query = self._apply_sort(query, sort)
        watch.start("rows_query")
        rows = query.offset(max(page - 1, 0) * page_size).limit(page_size).all()
        rows_ms = watch.stop("rows_query")
        watch.start("ai_tag_flag_query")
        result = self._with_ai_tag_flags(rows)
        ai_ms = watch.stop("ai_tag_flag_query")
        self.last_section_ms = {
            "total_count_query_ms": count_ms,
            "rows_query_ms": rows_ms,
            "ai_tag_flag_query_ms": ai_ms,
        }
        log_perf(
            "gallery_repository_search",
            total_count_query_ms=count_ms,
            rows_query_ms=rows_ms,
            ai_tag_flag_query_ms=ai_ms,
            row_count=len(rows),
            total=total,
            elapsed_ms=watch.total_ms(),
        )
        return result, total

    def map(
        self,
        *,
        service_name: str | None = None,
        year: int | None = None,
    ) -> list[tuple[CommonFile, CommonFileMetadata]]:
        """GPS가 있는 사진 Marker 목록을 조회한다."""
        query = (
            self.db.query(CommonFile, CommonFileMetadata)
            .join(
                CommonFileMetadata,
                CommonFileMetadata.file_id == CommonFile.id,
            )
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileMetadata.gps_lat.isnot(None))
            .filter(CommonFileMetadata.gps_lon.isnot(None))
        )
        query = self._filter_by_service(query, service_name)
        if year is not None:
            query = query.filter(
                func.extract("year", CommonFileMetadata.datetime_original) == year
            )
        return query.order_by(CommonFileMetadata.datetime_original.desc()).all()

    def timeline(
        self,
        *,
        service_name: str | None = None,
    ) -> list[tuple[int, int]]:
        """년도별 사진 수를 조회한다."""
        year_expr = func.extract("year", CommonFileMetadata.datetime_original)
        query = (
            self.db.query(
                year_expr.label("year"),
                func.count(CommonFile.id).label("count"),
            )
            .join(
                CommonFileMetadata,
                CommonFileMetadata.file_id == CommonFile.id,
            )
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileMetadata.datetime_original.isnot(None))
        )
        query = self._filter_by_service(query, service_name)
        rows = (
            query.group_by(year_expr)
            .order_by(year_expr.desc())
            .all()
        )
        return [(int(year), int(count)) for year, count in rows if year is not None]

    def statistics(
        self,
        *,
        service_name: str | None = None,
    ) -> dict[str, Any]:
        """Gallery 통계를 조회한다."""
        files_query = self.db.query(CommonFile).filter(CommonFile.deleted.is_(False))
        files_query = self._filter_by_service(files_query, service_name)

        total_photos = files_query.count()

        gps_query = (
            self.db.query(func.count(CommonFile.id))
            .join(
                CommonFileMetadata,
                CommonFileMetadata.file_id == CommonFile.id,
            )
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileMetadata.gps_lat.isnot(None))
            .filter(CommonFileMetadata.gps_lon.isnot(None))
        )
        gps_query = self._filter_by_service(gps_query, service_name)
        gps_count = gps_query.scalar() or 0

        ai_tag_query = (
            self.db.query(func.count(func.distinct(CommonFileTag.id)))
            .join(CommonFile, CommonFile.id == CommonFileTag.file_id)
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileTag.deleted.is_(False))
            .filter(CommonFileTag.source == TagSource.AI)
        )
        ai_tag_query = self._filter_by_service(ai_tag_query, service_name)
        ai_tag_count = ai_tag_query.scalar() or 0

        return {
            "total_photos": int(total_photos),
            "gps_count": int(gps_count),
            "ai_tag_count": int(ai_tag_count),
            "by_camera": self._group_counts(
                CommonFileMetadata.camera_model,
                service_name=service_name,
            ),
            "by_country": self._group_counts(
                CommonFileMetadata.country,
                service_name=service_name,
            ),
            "by_year": [
                {"name": str(year), "count": count}
                for year, count in self.timeline(service_name=service_name)
            ],
            "by_service": self._service_counts(service_name=service_name),
        }

    def _base_query(
        self,
        *,
        service_name: str | None = None,
    ) -> Query:
        query = (
            self.db.query(CommonFile, CommonFileMetadata)
            .outerjoin(
                CommonFileMetadata,
                CommonFileMetadata.file_id == CommonFile.id,
            )
            .outerjoin(
                MemoryKeeperFileState,
                MemoryKeeperFileState.file_id == CommonFile.id,
            )
            .filter(CommonFile.deleted.is_(False))
        )
        return self._filter_by_service(query, service_name)

    def _apply_filters(
        self,
        query: Query,
        *,
        year: int | None,
        country: str | None,
        city: str | None,
        camera: str | None,
        tag: str | None,
        favorite: bool | None,
        date_from: datetime | None,
        date_to: datetime | None,
        keyword: str | None,
        incomplete: bool | None,
        service_name: str | None,
    ) -> Query:
        if year is not None:
            query = query.filter(
                func.extract("year", CommonFileMetadata.datetime_original) == year
            )
        if country:
            query = query.filter(CommonFileMetadata.country == country)
        if city:
            query = query.filter(CommonFileMetadata.city == city)
        if camera:
            query = query.filter(CommonFileMetadata.camera_model.ilike(f"%{camera}%"))
        if favorite is not None:
            if (service_name or "").casefold() == "memorykeeper":
                query = query.filter(
                    func.coalesce(
                        MemoryKeeperFileState.favorite,
                        CommonFile.favorite,
                    ).is_(favorite)
                )
            else:
                query = query.filter(CommonFile.favorite.is_(favorite))
        if date_from is not None:
            query = query.filter(CommonFileMetadata.datetime_original >= date_from)
        if date_to is not None:
            query = query.filter(CommonFileMetadata.datetime_original <= date_to)
        if tag:
            tag_exists = (
                self.db.query(CommonFileTag.id)
                .filter(CommonFileTag.file_id == CommonFile.id)
                .filter(CommonFileTag.deleted.is_(False))
                .filter(CommonFileTag.tag.ilike(f"%{tag}%"))
                .exists()
            )
            query = query.filter(tag_exists)
        if keyword:
            like = f"%{keyword}%"
            tag_exists = (
                self.db.query(CommonFileTag.id)
                .filter(CommonFileTag.file_id == CommonFile.id)
                .filter(CommonFileTag.deleted.is_(False))
                .filter(CommonFileTag.tag.ilike(like))
                .exists()
            )
            query = query.filter(
                or_(
                    CommonFile.original_name.ilike(like),
                    CommonFileMetadata.country.ilike(like),
                    CommonFileMetadata.city.ilike(like),
                    CommonFileMetadata.place_name.ilike(like),
                    CommonFileMetadata.camera_model.ilike(like),
                    tag_exists,
                )
            )
        return self._apply_incomplete(
            query,
            incomplete=incomplete,
            service_name=service_name,
        )

    @staticmethod
    def _apply_incomplete(
        query: Query,
        *,
        incomplete: bool | None,
        service_name: str | None,
    ) -> Query:
        if incomplete is None:
            return query
        if (service_name or "").casefold() != "memorykeeper":
            return query.filter(False) if incomplete else query
        condition = CommonFileMetadata.memorykeeper_place_id.is_(None)
        return query.filter(condition if incomplete else ~condition)

    def _apply_sort(self, query: Query, sort: str) -> Query:
        normalized = (sort or "capture_datetime_desc").lower()
        if normalized in {"capture_datetime_asc", "datetime_asc"}:
            return query.order_by(
                CommonFileMetadata.datetime_original.asc().nullslast(),
                CommonFile.id.asc(),
            )
        if normalized in {"created_at_desc"}:
            return query.order_by(CommonFile.created_at.desc(), CommonFile.id.desc())
        if normalized in {"created_at_asc"}:
            return query.order_by(CommonFile.created_at.asc(), CommonFile.id.asc())
        if normalized in {"filename_asc"}:
            return query.order_by(CommonFile.original_name.asc(), CommonFile.id.asc())
        if normalized in {"filename_desc"}:
            return query.order_by(CommonFile.original_name.desc(), CommonFile.id.desc())
        return query.order_by(
            CommonFileMetadata.datetime_original.desc().nullslast(),
            CommonFile.id.desc(),
        )

    def _with_ai_tag_flags(
        self,
        rows: list[tuple[CommonFile, CommonFileMetadata | None]],
    ) -> list[tuple[CommonFile, CommonFileMetadata | None, bool]]:
        if not rows:
            return []
        file_ids = [file.id for file, _ in rows]
        ai_rows = (
            self.db.query(CommonFileTag.file_id)
            .filter(CommonFileTag.file_id.in_(file_ids))
            .filter(CommonFileTag.deleted.is_(False))
            .filter(CommonFileTag.source == TagSource.AI)
            .distinct()
            .all()
        )
        ai_ids = {row[0] for row in ai_rows}
        return [
            (file, metadata, file.id in ai_ids)
            for file, metadata in rows
        ]

    def _group_counts(
        self,
        column: Any,
        *,
        service_name: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            self.db.query(column.label("name"), func.count(CommonFile.id).label("count"))
            .join(
                CommonFileMetadata,
                CommonFileMetadata.file_id == CommonFile.id,
            )
            .filter(CommonFile.deleted.is_(False))
            .filter(column.isnot(None))
            .filter(column != "")
        )
        query = self._filter_by_service(query, service_name)
        rows = query.group_by(column).order_by(func.count(CommonFile.id).desc()).all()
        return [{"name": str(name), "count": int(count)} for name, count in rows]

    def _service_counts(
        self,
        *,
        service_name: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            self.db.query(
                CommonFileService.service_name.label("name"),
                func.count(CommonFile.id).label("count"),
            )
            .join(CommonFile, CommonFile.id == CommonFileService.file_id)
            .filter(CommonFile.deleted.is_(False))
        )
        if service_name:
            query = query.filter(CommonFileService.service_name == service_name)
        rows = (
            query.group_by(CommonFileService.service_name)
            .order_by(func.count(CommonFile.id).desc())
            .all()
        )
        return [{"name": str(name), "count": int(count)} for name, count in rows]

    @staticmethod
    def _filter_by_service(query: Query, service_name: str | None) -> Query:
        if not service_name:
            return query
        return (
            query.join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .filter(CommonFileService.service_name == service_name)
        )
