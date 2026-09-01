"""Set-based queries for MemoryKeeper TravelRecords fast reads."""

from __future__ import annotations

from datetime import date

from sqlalchemy import and_, case, extract, func, select
from sqlalchemy.orm import Query, Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.place import MemoryKeeperPlace


class MemoryKeeperFastTravelRepository:
    """Aggregate canonical MemoryKeeper capture dates without photo snapshots."""

    SERVICE_NAME = "MemoryKeeper"

    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _country_expression():
        return func.coalesce(MemoryKeeperPlace.country, CommonFileMetadata.country)

    @staticmethod
    def _region_expression():
        return func.coalesce(
            MemoryKeeperPlace.city,
            MemoryKeeperPlace.province,
            CommonFileMetadata.city,
            CommonFileMetadata.province,
        )

    @staticmethod
    def _place_display_expression():
        return func.coalesce(
            MemoryKeeperPlace.display_name,
            CommonFileMetadata.place_name,
        )

    @staticmethod
    def _media_priority_expression():
        return case(
            (
                and_(CommonFile.thumb_path.isnot(None), CommonFile.thumb_path != ""),
                2,
            ),
            (
                and_(
                    CommonFile.preview_path.isnot(None),
                    CommonFile.preview_path != "",
                ),
                1,
            ),
            else_=0,
        )

    def _base_query(self) -> Query:
        return (
            self.db.query(CommonFile.id)
            .select_from(MemoryKeeperFileState)
            .join(CommonFile, CommonFile.id == MemoryKeeperFileState.file_id)
            .join(
                CommonFileService,
                CommonFileService.file_id == CommonFile.id,
            )
            .outerjoin(
                CommonFileMetadata,
                CommonFileMetadata.file_id == CommonFile.id,
            )
            .outerjoin(
                MemoryKeeperPlace,
                and_(
                    MemoryKeeperPlace.id == CommonFileMetadata.memorykeeper_place_id,
                    MemoryKeeperPlace.deleted_at.is_(None),
                ),
            )
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .filter(CommonFile.deleted.is_(False))
            .filter(MemoryKeeperFileState.effective_capture_date.isnot(None))
        )

    def place_date_rows(self) -> list[object]:
        return list(self.db.execute(self.build_place_date_statement()).all())

    def build_place_date_statement(self):
        country = self._country_expression()
        region = self._region_expression()
        display_name = self._place_display_expression()
        group_columns = [
            CommonFileMetadata.memorykeeper_place_id,
            display_name,
            country,
            region,
        ]
        ranked = (
            self._base_query()
            .with_entities(
                CommonFileMetadata.memorykeeper_place_id.label(
                    "memorykeeper_place_id"
                ),
                display_name.label("place_display_name"),
                country.label("country"),
                region.label("region"),
                MemoryKeeperFileState.effective_capture_date,
                CommonFile.id.label("common_file_id"),
                CommonFile.file_id,
                CommonFile.preview_path,
                CommonFile.thumb_path,
                func.row_number()
                .over(
                    partition_by=group_columns,
                    order_by=self._representative_order(),
                )
                .label("representative_rank"),
            )
            .subquery("travel_place_ranked")
        )
        return (
            select(
                ranked.c.memorykeeper_place_id,
                ranked.c.place_display_name,
                ranked.c.country,
                ranked.c.region,
                ranked.c.effective_capture_date,
                func.count(func.distinct(ranked.c.common_file_id)).label(
                    "photo_count"
                ),
                *self._representative_aggregate_columns(ranked),
            )
            .group_by(
                ranked.c.memorykeeper_place_id,
                ranked.c.place_display_name,
                ranked.c.country,
                ranked.c.region,
                ranked.c.effective_capture_date,
            )
            .order_by(
                ranked.c.country.asc().nulls_last(),
                ranked.c.region.asc().nulls_last(),
                ranked.c.place_display_name.asc().nulls_last(),
                ranked.c.memorykeeper_place_id.asc().nulls_last(),
                ranked.c.effective_capture_date.asc(),
            )
        )

    def country_date_rows(self) -> list[object]:
        return list(self.db.execute(self.build_country_date_statement()).all())

    def build_country_date_statement(self):
        country = self._country_expression()
        ranked = (
            self._base_query()
            .with_entities(
                country.label("country"),
                MemoryKeeperFileState.effective_capture_date,
                CommonFile.id.label("common_file_id"),
                CommonFile.file_id,
                CommonFile.preview_path,
                CommonFile.thumb_path,
                func.row_number()
                .over(
                    partition_by=[country],
                    order_by=self._representative_order(),
                )
                .label("representative_rank"),
            )
            .subquery("travel_country_ranked")
        )
        return (
            select(
                ranked.c.country,
                ranked.c.effective_capture_date,
                func.count(func.distinct(ranked.c.common_file_id)).label(
                    "photo_count"
                ),
                *self._representative_aggregate_columns(ranked),
            )
            .group_by(ranked.c.country, ranked.c.effective_capture_date)
            .order_by(
                ranked.c.country.asc().nulls_last(),
                ranked.c.effective_capture_date.asc(),
            )
        )

    def exact_anniversary_candidates(self, reference_date: date) -> list[object]:
        return list(
            self.db.execute(
                self.build_exact_anniversary_statement(reference_date)
            ).all()
        )

    def build_exact_anniversary_statement(self, reference_date: date):
        condition = and_(
            MemoryKeeperFileState.effective_capture_date < reference_date,
            extract("month", MemoryKeeperFileState.effective_capture_date)
            == reference_date.month,
            extract("day", MemoryKeeperFileState.effective_capture_date)
            == reference_date.day,
        )
        return self._memory_candidate_statement(condition)

    def previous_year_period_candidates(
        self,
        *,
        date_from: date,
        date_to: date,
        reference_date: date,
    ) -> list[object]:
        return list(
            self.db.execute(
                self.build_previous_year_period_statement(
                    date_from=date_from,
                    date_to=date_to,
                    reference_date=reference_date,
                )
            ).all()
        )

    def build_previous_year_period_statement(
        self,
        *,
        date_from: date,
        date_to: date,
        reference_date: date,
    ):
        condition = and_(
            MemoryKeeperFileState.effective_capture_date >= date_from,
            MemoryKeeperFileState.effective_capture_date <= date_to,
            MemoryKeeperFileState.effective_capture_date < reference_date,
        )
        return self._memory_candidate_statement(condition)

    def _representative_order(self) -> list[object]:
        return [
            self._media_priority_expression().desc(),
            MemoryKeeperFileState.effective_capture_datetime.desc(),
            CommonFile.id.desc(),
        ]

    @staticmethod
    def _representative_aggregate_columns(ranked):
        is_representative = ranked.c.representative_rank == 1
        return (
            func.max(
                case((is_representative, ranked.c.common_file_id))
            ).label("representative_common_file_id"),
            func.max(case((is_representative, ranked.c.file_id))).label(
                "representative_file_id"
            ),
            func.max(case((is_representative, ranked.c.preview_path))).label(
                "representative_preview_path"
            ),
            func.max(case((is_representative, ranked.c.thumb_path))).label(
                "representative_thumb_path"
            ),
            func.max(
                case((is_representative, ranked.c.effective_capture_date))
            ).label("representative_capture_date"),
        )

    def _memory_candidate_statement(self, condition: object):
        country = self._country_expression()
        display_name = self._place_display_expression()
        ranked = (
            self._base_query()
            .filter(condition)
            .with_entities(
                CommonFile.id.label("common_file_id"),
                CommonFile.file_id,
                CommonFile.preview_path,
                CommonFile.thumb_path,
                MemoryKeeperFileState.effective_capture_datetime,
                MemoryKeeperFileState.effective_capture_date,
                CommonFileMetadata.memorykeeper_place_id,
                display_name.label("place_display_name"),
                country.label("country"),
                func.row_number()
                .over(
                    partition_by=[MemoryKeeperFileState.effective_capture_date],
                    order_by=self._representative_order(),
                )
                .label("candidate_rank"),
            )
            .subquery("travel_memory_candidates")
        )
        return (
            select(ranked)
            .where(ranked.c.candidate_rank == 1)
            .order_by(
                ranked.c.effective_capture_date.desc(),
                ranked.c.common_file_id.desc(),
            )
        )
