"""Set-based authoritative projections for MemoryKeeper cleanup queues."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Query, Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.repositories.place_cleanup_repository import (
    hierarchy_unclassified_condition,
    pending_condition,
    place_cleanup_condition,
)


class CaptureDateCleanupReason:
    MISSING = "MISSING_CAPTURE_DATE"
    FALLBACK = "FALLBACK_DATE_REQUIRES_REVIEW"
    INVALID = "INVALID_CAPTURE_DATE"


class MemoryKeeperCleanupGroupRepository:
    SERVICE_NAME = "MemoryKeeper"

    def __init__(self, db: Session) -> None:
        self.db = db

    def _base_query(self) -> Query:
        return (
            self.db.query(CommonFile.id)
            .select_from(CommonFile)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .outerjoin(CommonFileMetadata, CommonFileMetadata.file_id == CommonFile.id)
            .outerjoin(MemoryKeeperFileState, MemoryKeeperFileState.file_id == CommonFile.id)
            .outerjoin(
                MemoryKeeperPlace,
                and_(
                    MemoryKeeperPlace.id == CommonFileMetadata.memorykeeper_place_id,
                    MemoryKeeperPlace.deleted_at.is_(None),
                ),
            )
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .filter(CommonFile.deleted.is_(False))
        )

    @staticmethod
    def _place_issue_expression():
        return case(
            (pending_condition(), "PLACE_UNASSIGNED"),
            (hierarchy_unclassified_condition(), "HIERARCHY_INCOMPLETE"),
            else_="RESOLVED",
        )

    @staticmethod
    def _date_reason_expression():
        return case(
            (
                or_(
                    MemoryKeeperFileState.effective_capture_datetime.is_(None),
                    MemoryKeeperFileState.date_basis.is_(None),
                ),
                CaptureDateCleanupReason.MISSING,
            ),
            (
                MemoryKeeperFileState.date_basis.in_(("IMPORTED", "CREATED")),
                CaptureDateCleanupReason.FALLBACK,
            ),
            else_=None,
        )

    @classmethod
    def _date_cleanup_condition(cls):
        return cls._date_reason_expression().isnot(None)

    @staticmethod
    def _cursor_filter(query: Query, grouped, cursor: tuple[datetime | None, int] | None) -> Query:
        if cursor is None:
            return query
        captured_at, tie_breaker = cursor
        if captured_at is None:
            return query.filter(
                grouped.c.last_capture.is_(None),
                grouped.c.tie_breaker < tie_breaker,
            )
        return query.filter(
            or_(
                grouped.c.last_capture < captured_at,
                grouped.c.last_capture.is_(None),
                and_(
                    grouped.c.last_capture == captured_at,
                    grouped.c.tie_breaker < tie_breaker,
                ),
            )
        )

    @staticmethod
    def _value_match(column, value):
        return column.is_(None) if value is None else column == value

    def place_groups(
        self,
        *,
        limit: int,
        cursor: tuple[datetime | None, int] | None,
    ) -> tuple[list[object], int, int]:
        issue = self._place_issue_expression()
        key_columns = (
            issue,
            MemoryKeeperFileState.effective_capture_date,
            CommonFileMetadata.gps_lat,
            CommonFileMetadata.gps_lon,
            CommonFileMetadata.country,
            CommonFileMetadata.province,
            CommonFileMetadata.city,
            CommonFileMetadata.district,
            CommonFileMetadata.place_name,
        )
        grouped = (
            self._base_query()
            .with_entities(
                issue.label("issue_type"),
                MemoryKeeperFileState.effective_capture_date.label("capture_date"),
                CommonFileMetadata.gps_lat.label("gps_lat"),
                CommonFileMetadata.gps_lon.label("gps_lon"),
                CommonFileMetadata.country.label("country"),
                CommonFileMetadata.province.label("province"),
                CommonFileMetadata.city.label("city"),
                CommonFileMetadata.district.label("district"),
                CommonFileMetadata.place_name.label("place_name"),
                func.count(CommonFile.id).label("media_count"),
                func.min(MemoryKeeperFileState.effective_capture_datetime).label("first_capture"),
                func.max(MemoryKeeperFileState.effective_capture_datetime).label("last_capture"),
                func.max(CommonFile.id).label("tie_breaker"),
            )
            .filter(place_cleanup_condition())
            .group_by(*key_columns)
            .subquery("place_cleanup_groups")
        )
        totals = self.db.query(
            func.count(grouped.c.tie_breaker),
            func.coalesce(func.sum(grouped.c.media_count), 0),
        ).one()
        page = self.db.query(
            *grouped.c,
            CommonFile.file_id.label("representative_file_id"),
            CommonFile.thumb_path.label("representative_thumb_path"),
        ).join(CommonFile, CommonFile.id == grouped.c.tie_breaker)
        page = self._cursor_filter(page, grouped, cursor)
        rows = (
            page.order_by(
                grouped.c.last_capture.desc().nullslast(),
                grouped.c.tie_breaker.desc(),
            )
            .limit(limit + 1)
            .all()
        )
        return rows, int(totals[0] or 0), int(totals[1] or 0)

    def place_group_photos(
        self,
        *,
        key: dict[str, object],
        limit: int,
        cursor: tuple[datetime | None, int] | None,
    ) -> tuple[list[object], int]:
        expected = {
            "issue_type", "capture_date", "gps_lat", "gps_lon", "country",
            "province", "city", "district", "place_name",
        }
        if set(key) != expected:
            return [], 0
        if key["issue_type"] not in {"PLACE_UNASSIGNED", "HIERARCHY_INCOMPLETE"}:
            return [], 0
        if any(
            value is not None and not isinstance(value, str)
            for name, value in key.items()
            if name not in {"gps_lat", "gps_lon"}
        ):
            return [], 0
        if any(
            value is not None
            and (not isinstance(value, (int, float)) or isinstance(value, bool))
            for value in (key["gps_lat"], key["gps_lon"])
        ):
            return [], 0
        try:
            capture_date = (
                date.fromisoformat(str(key["capture_date"]))
                if key["capture_date"]
                else None
            )
        except (TypeError, ValueError):
            return [], 0
        query = self._base_query().with_entities(
            CommonFile.id.label("common_file_id"),
            CommonFile.file_id,
            CommonFile.thumb_path,
            MemoryKeeperFileState.effective_capture_datetime,
            CommonFileMetadata.gps_lat,
            CommonFileMetadata.gps_lon,
            CommonFileMetadata.country,
            CommonFileMetadata.province,
            CommonFileMetadata.city,
            CommonFileMetadata.district,
            CommonFileMetadata.place_name,
            CommonFileMetadata.memorykeeper_place_id,
            CommonFileMetadata.place_match_revision,
        ).filter(
            place_cleanup_condition(),
            self._place_issue_expression() == key["issue_type"],
            self._value_match(MemoryKeeperFileState.effective_capture_date, capture_date),
            self._value_match(CommonFileMetadata.gps_lat, key["gps_lat"]),
            self._value_match(CommonFileMetadata.gps_lon, key["gps_lon"]),
            self._value_match(CommonFileMetadata.country, key["country"]),
            self._value_match(CommonFileMetadata.province, key["province"]),
            self._value_match(CommonFileMetadata.city, key["city"]),
            self._value_match(CommonFileMetadata.district, key["district"]),
            self._value_match(CommonFileMetadata.place_name, key["place_name"]),
        )
        total = query.order_by(None).count()
        query = self._photo_cursor_filter(query, cursor)
        return (
            query.order_by(
                MemoryKeeperFileState.effective_capture_datetime.desc().nullslast(),
                CommonFile.id.desc(),
            ).limit(limit + 1).all(),
            total,
        )

    def date_groups(
        self,
        *,
        limit: int,
        cursor: tuple[datetime | None, int] | None,
    ) -> tuple[list[object], int, int]:
        reason = self._date_reason_expression()
        grouped = (
            self._base_query()
            .with_entities(
                reason.label("cleanup_reason"),
                MemoryKeeperFileState.date_basis.label("date_basis"),
                MemoryKeeperFileState.effective_capture_date.label("capture_date"),
                func.count(CommonFile.id).label("media_count"),
                func.min(MemoryKeeperFileState.effective_capture_datetime).label("first_capture"),
                func.max(MemoryKeeperFileState.effective_capture_datetime).label("last_capture"),
                func.max(CommonFile.id).label("tie_breaker"),
            )
            .filter(self._date_cleanup_condition())
            .group_by(
                reason,
                MemoryKeeperFileState.date_basis,
                MemoryKeeperFileState.effective_capture_date,
            )
            .subquery("capture_date_cleanup_groups")
        )
        totals = self.db.query(
            func.count(grouped.c.tie_breaker),
            func.coalesce(func.sum(grouped.c.media_count), 0),
        ).one()
        page = self.db.query(
            *grouped.c,
            CommonFile.file_id.label("representative_file_id"),
            CommonFile.thumb_path.label("representative_thumb_path"),
        ).join(CommonFile, CommonFile.id == grouped.c.tie_breaker)
        page = self._cursor_filter(page, grouped, cursor)
        rows = page.order_by(
            grouped.c.last_capture.desc().nullslast(),
            grouped.c.tie_breaker.desc(),
        ).limit(limit + 1).all()
        return rows, int(totals[0] or 0), int(totals[1] or 0)

    def date_group_photos(
        self,
        *,
        key: dict[str, object],
        limit: int,
        cursor: tuple[datetime | None, int] | None,
    ) -> tuple[list[object], int]:
        if set(key) != {"cleanup_reason", "date_basis", "capture_date"}:
            return [], 0
        if key["cleanup_reason"] not in {
            CaptureDateCleanupReason.MISSING,
            CaptureDateCleanupReason.FALLBACK,
        }:
            return [], 0
        if key["date_basis"] not in {None, "IMPORTED", "CREATED"}:
            return [], 0
        if key["capture_date"] is not None and not isinstance(key["capture_date"], str):
            return [], 0
        try:
            capture_date = (
                date.fromisoformat(str(key["capture_date"]))
                if key["capture_date"]
                else None
            )
        except (TypeError, ValueError):
            return [], 0
        query = self._base_query().with_entities(
            CommonFile.id.label("common_file_id"),
            CommonFile.file_id,
            CommonFile.thumb_path,
            CommonFileMetadata.original_capture_datetime,
            MemoryKeeperFileState.user_capture_datetime,
            MemoryKeeperFileState.user_capture_precision,
            MemoryKeeperFileState.effective_capture_datetime,
            MemoryKeeperFileState.effective_capture_date,
            MemoryKeeperFileState.effective_capture_year,
            MemoryKeeperFileState.date_basis,
            MemoryKeeperFileState.revision.label("date_revision"),
            self._date_reason_expression().label("cleanup_reason"),
        ).filter(
            self._date_cleanup_condition(),
            self._date_reason_expression() == key["cleanup_reason"],
            self._value_match(MemoryKeeperFileState.date_basis, key["date_basis"]),
            self._value_match(MemoryKeeperFileState.effective_capture_date, capture_date),
        )
        total = query.order_by(None).count()
        query = self._photo_cursor_filter(query, cursor)
        return (
            query.order_by(
                MemoryKeeperFileState.effective_capture_datetime.desc().nullslast(),
                CommonFile.id.desc(),
            ).limit(limit + 1).all(),
            total,
        )

    @staticmethod
    def _photo_cursor_filter(
        query: Query,
        cursor: tuple[datetime | None, int] | None,
    ) -> Query:
        if cursor is None:
            return query
        captured_at, file_id = cursor
        if captured_at is None:
            return query.filter(
                MemoryKeeperFileState.effective_capture_datetime.is_(None),
                CommonFile.id < file_id,
            )
        return query.filter(
            or_(
                MemoryKeeperFileState.effective_capture_datetime < captured_at,
                MemoryKeeperFileState.effective_capture_datetime.is_(None),
                and_(
                    MemoryKeeperFileState.effective_capture_datetime == captured_at,
                    CommonFile.id < file_id,
                ),
            )
        )
