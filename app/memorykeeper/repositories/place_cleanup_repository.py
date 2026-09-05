"""Queries for the user-facing MemoryKeeper place-cleanup work set."""

from __future__ import annotations

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Query, Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.place import MemoryKeeperPlace


def memorykeeper_country_expression():
    return func.coalesce(
        MemoryKeeperPlace.country,
        CommonFileMetadata.country,
    )


def memorykeeper_region_expression():
    return func.coalesce(
        MemoryKeeperPlace.city,
        MemoryKeeperPlace.province,
        CommonFileMetadata.city,
        CommonFileMetadata.province,
    )


def memorykeeper_place_display_expression():
    return func.coalesce(
        MemoryKeeperPlace.display_name,
        CommonFileMetadata.place_name,
    )


def pending_condition():
    return CommonFileMetadata.memorykeeper_place_id.is_(None)


def _missing_hierarchy_value(expression):
    return func.nullif(func.trim(expression), "").is_(None)


def hierarchy_unclassified_condition():
    """Match canonical Gallery rows without a usable hierarchy label."""
    return and_(
        MemoryKeeperFileState.effective_capture_datetime.isnot(None),
        or_(
            _missing_hierarchy_value(memorykeeper_country_expression()),
            _missing_hierarchy_value(memorykeeper_region_expression()),
            _missing_hierarchy_value(memorykeeper_place_display_expression()),
        ),
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
