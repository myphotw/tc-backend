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
    def _latitude_expression():
        return func.coalesce(
            MemoryKeeperPlace.latitude,
            CommonFileMetadata.gps_lat,
        )

    @staticmethod
    def _longitude_expression():
        return func.coalesce(
            MemoryKeeperPlace.longitude,
            CommonFileMetadata.gps_lon,
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
        latitude = self._latitude_expression()
        longitude = self._longitude_expression()
        group_columns = [
            MemoryKeeperPlace.id,
            display_name,
            country,
            region,
        ]
        has_coordinates = case(
            (
                and_(latitude.isnot(None), longitude.isnot(None)),
                1,
            ),
            else_=0,
        )
        ranked = (
            self._base_query()
            .with_entities(
                MemoryKeeperPlace.id.label("memorykeeper_place_id"),
                display_name.label("place_display_name"),
                country.label("country"),
                region.label("region"),
                latitude.label("latitude"),
                longitude.label("longitude"),
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
                func.row_number()
                .over(
                    partition_by=group_columns,
                    order_by=[
                        has_coordinates.desc(),
                        MemoryKeeperFileState.effective_capture_datetime.desc(),
                        CommonFile.id.desc(),
                    ],
                )
                .label("coordinate_rank"),
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
                *self._coordinate_aggregate_columns(ranked),
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

    def past_year_period_candidates(
        self,
        *,
        reference_date: date,
        period_from: date,
        period_to: date,
        limit: int,
    ) -> list[object]:
        """Return bounded nearby-calendar candidates from two or more years ago."""
        return list(
            self.db.execute(
                self.build_past_year_period_statement(
                    reference_date=reference_date,
                    period_from=period_from,
                    period_to=period_to,
                    limit=limit,
                )
            ).all()
        )

    def build_past_year_period_statement(
        self,
        *,
        reference_date: date,
        period_from: date,
        period_to: date,
        limit: int,
    ):
        condition = and_(
            MemoryKeeperFileState.effective_capture_date
            < date(reference_date.year - 1, 1, 1),
            self._month_day_window_condition(
                MemoryKeeperFileState.effective_capture_date,
                period_from=period_from,
                period_to=period_to,
            ),
        )
        return self._memory_candidate_statement(condition, limit=limit)

    @staticmethod
    def _month_day_window_condition(
        column: object,
        *,
        period_from: date,
        period_to: date,
    ):
        """Compare recurring month/day ranges without database-specific date casts."""
        month_day = extract("month", column) * 100 + extract("day", column)
        start = period_from.month * 100 + period_from.day
        end = period_to.month * 100 + period_to.day
        if start <= end:
            return and_(month_day >= start, month_day <= end)
        return (month_day >= start) | (month_day <= end)

    def long_ago_candidates(
        self,
        *,
        reference_date: date,
        limit: int,
    ) -> list[object]:
        """Return bounded oldest real capture dates as a deterministic fallback."""
        return list(
            self.db.execute(
                self.build_long_ago_statement(
                    reference_date=reference_date,
                    limit=limit,
                )
            ).all()
        )

    def build_long_ago_statement(
        self,
        *,
        reference_date: date,
        limit: int,
    ):
        condition = (
            MemoryKeeperFileState.effective_capture_date
            < date(reference_date.year - 1, 1, 1)
        )
        return self._memory_candidate_statement(
            condition,
            descending=False,
            limit=limit,
        )

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

    @staticmethod
    def _coordinate_aggregate_columns(ranked):
        is_coordinate_representative = ranked.c.coordinate_rank == 1
        return (
            func.max(
                case((is_coordinate_representative, ranked.c.latitude))
            ).label("latitude"),
            func.max(
                case((is_coordinate_representative, ranked.c.longitude))
            ).label("longitude"),
        )

    def _memory_candidate_statement(
        self,
        condition: object,
        *,
        descending: bool = True,
        limit: int | None = None,
    ):
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
        effective_date_order = (
            ranked.c.effective_capture_date.desc()
            if descending
            else ranked.c.effective_capture_date.asc()
        )
        statement = (
            select(ranked)
            .where(ranked.c.candidate_rank == 1)
            .order_by(
                effective_date_order,
                (
                    ranked.c.common_file_id.desc()
                    if descending
                    else ranked.c.common_file_id.asc()
                ),
            )
        )
        return statement.limit(limit) if limit is not None else statement
