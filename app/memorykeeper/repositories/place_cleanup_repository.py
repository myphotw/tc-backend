"""Queries for the user-facing MemoryKeeper place-cleanup work set."""

from __future__ import annotations

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Query, Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.place import MemoryKeeperPlace


LOCATION_REASON_PLACE_UNASSIGNED = "PLACE_UNASSIGNED"
LOCATION_REASON_COUNTRY_MISSING = "COUNTRY_MISSING"
LOCATION_REASON_REGION_MISSING = "REGION_MISSING"
LOCATION_REASON_PLACE_MISSING = "PLACE_MISSING"


def memorykeeper_country_expression(
    *, place=MemoryKeeperPlace, metadata=CommonFileMetadata
):
    return func.coalesce(
        place.country,
        metadata.country,
    )


def memorykeeper_region_expression(
    *, place=MemoryKeeperPlace, metadata=CommonFileMetadata
):
    return func.coalesce(
        place.city,
        place.province,
        metadata.city,
        metadata.province,
    )


def memorykeeper_place_display_expression(
    *, place=MemoryKeeperPlace, metadata=CommonFileMetadata
):
    return func.coalesce(
        place.display_name,
        metadata.place_name,
    )


def memorykeeper_location_values(
    *, place: object | None, metadata: object
) -> tuple[object | None, object | None, object | None]:
    """Mirror the canonical SQL coalesce order for non-query response mapping."""

    def value(source: object | None, name: str) -> object | None:
        return getattr(source, name, None) if source is not None else None

    def first_not_none(*values: object | None) -> object | None:
        return next((item for item in values if item is not None), None)

    country = first_not_none(value(place, "country"), value(metadata, "country"))
    region = first_not_none(
        value(place, "city"),
        value(place, "province"),
        value(metadata, "city"),
        value(metadata, "province"),
    )
    display_name = first_not_none(
        value(place, "display_name"),
        value(metadata, "place_name"),
    )
    return country, region, display_name


def location_hierarchy_reason(
    *,
    registered_place_id: object | None,
    country: object | None,
    region: object | None,
    place: object | None,
) -> str | None:
    """Explain an incomplete canonical hierarchy without inferring location data."""

    def missing(value: object | None) -> bool:
        return value is None or not str(value).strip()

    if not any(missing(value) for value in (country, region, place)):
        return None
    if registered_place_id is None:
        return LOCATION_REASON_PLACE_UNASSIGNED
    if missing(country):
        return LOCATION_REASON_COUNTRY_MISSING
    if missing(region):
        return LOCATION_REASON_REGION_MISSING
    return LOCATION_REASON_PLACE_MISSING


def pending_condition():
    return and_(
        normal_photo_condition(),
        CommonFileMetadata.memorykeeper_place_id.is_(None),
    )


def normal_photo_condition():
    """Missing legacy state remains NORMAL for backward-compatible reads."""
    return func.coalesce(MemoryKeeperFileState.photo_category, "NORMAL") == "NORMAL"


def _missing_hierarchy_value(expression):
    return func.nullif(func.trim(expression), "").is_(None)


def location_hierarchy_complete_condition():
    """Return whether canonical country, region, and place labels are usable."""
    return and_(
        ~_missing_hierarchy_value(memorykeeper_country_expression()),
        ~_missing_hierarchy_value(memorykeeper_region_expression()),
        ~_missing_hierarchy_value(memorykeeper_place_display_expression()),
    )


def location_hierarchy_incomplete_condition():
    """Classify NORMAL photos whose canonical Gallery hierarchy is incomplete."""
    return and_(
        normal_photo_condition(),
        ~location_hierarchy_complete_condition(),
    )


def hierarchy_unclassified_condition():
    """Keep the legacy cleanup queue scoped to exact-date photos."""
    return and_(
        location_hierarchy_incomplete_condition(),
        MemoryKeeperFileState.effective_capture_datetime.isnot(None),
    )


def place_cleanup_condition():
    return or_(pending_condition(), hierarchy_unclassified_condition())


class MemoryKeeperPlaceCleanupRepository:
    SERVICE_NAME = "MemoryKeeper"

    def __init__(self, db: Session) -> None:
        self.db = db

    def _base_query(self) -> Query:
        return (
            self.db.query(CommonFile, CommonFileMetadata)
            .select_from(CommonFile)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .outerjoin(
                CommonFileMetadata,
                CommonFileMetadata.file_id == CommonFile.id,
            )
            .outerjoin(
                MemoryKeeperFileState,
                MemoryKeeperFileState.file_id == CommonFile.id,
            )
            .outerjoin(
                MemoryKeeperPlace,
                and_(
                    MemoryKeeperPlace.id
                    == CommonFileMetadata.memorykeeper_place_id,
                    MemoryKeeperPlace.deleted_at.is_(None),
                ),
            )
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .filter(CommonFile.deleted.is_(False))
        )

    def summary_counts(self) -> tuple[int, int, int]:
        row = self._base_query().with_entities(
            func.count(CommonFile.id),
            func.coalesce(
                func.sum(case((pending_condition(), 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((place_cleanup_condition(), 1), else_=0)),
                0,
            ),
        ).one()
        return (
            int(row[0] or 0),
            int(row[1] or 0),
            int(row[2] or 0),
        )

    def list(
        self,
        *,
        page: int,
        page_size: int,
    ) -> tuple[list[tuple[CommonFile, CommonFileMetadata | None]], int]:
        query = self._base_query().filter(place_cleanup_condition())
        total = query.count()
        rows = (
            query.order_by(
                CommonFileMetadata.datetime_original.desc().nullslast(),
                CommonFile.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return rows, total
