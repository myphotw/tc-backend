"""Set-based query repository for the MemoryKeeper fast-read Gallery API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import and_, case, func, or_, select, true
from sqlalchemy.sql import Select
from sqlalchemy.orm import Query, Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.place import MemoryKeeperPlace


@dataclass(frozen=True)
class FastGalleryFilters:
    year: int | None = None
    country: str | None = None
    region: str | None = None
    place_id: str | None = None
    favorite: bool | None = None
    has_gps: bool | None = None
    date_from: date | None = None
    date_to: date | None = None


class MemoryKeeperFastGalleryRepository:
    """Use the MemoryKeeper capture projection, never common Gallery fallback."""

    SERVICE_NAME = "MemoryKeeper"

    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _country_expression():
        return func.coalesce(
            MemoryKeeperPlace.country,
            CommonFileMetadata.country,
        )

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
    def _has_gps_expression():
        return case(
            (
                and_(
                    CommonFileMetadata.gps_lat.isnot(None),
                    CommonFileMetadata.gps_lon.isnot(None),
                ),
                True,
            ),
            else_=False,
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
            # NULL projection rows are intentionally excluded.  They violate
            # the completed capture-date backfill invariant and belong in
            # diagnostics/backfill, not a canonical ordered Gallery list.
            .filter(MemoryKeeperFileState.effective_capture_datetime.isnot(None))
        )

    @staticmethod
    def _correlated_exists(statement: Select):
        """Keep a point lookup correlated instead of flattening it into a join.

        ``OFFSET 0`` is semantically neutral.  On PostgreSQL it prevents the
        service/file membership probes from becoming reorderable semi-joins
        that can move ahead of the ordered state index scan.
        """
        return statement.offset(0).exists()

    def _photo_candidates(
        self,
        *,
        filters: FastGalleryFilters,
        limit: int,
        cursor_datetime: datetime | None,
        cursor_file_id: int | None,
    ):
        active_file = self._correlated_exists(
            select(CommonFile.id).where(
                CommonFile.id == MemoryKeeperFileState.file_id,
                CommonFile.deleted.is_(False),
            )
        )
        memorykeeper_link = self._correlated_exists(
            select(CommonFileService.id).where(
                CommonFileService.file_id == MemoryKeeperFileState.file_id,
                CommonFileService.service_name == self.SERVICE_NAME,
            )
        )
        statement = (
            select(
                MemoryKeeperFileState.file_id.label("common_file_id"),
                MemoryKeeperFileState.favorite,
                MemoryKeeperFileState.effective_capture_datetime,
                MemoryKeeperFileState.effective_capture_date,
                MemoryKeeperFileState.effective_capture_year,
                MemoryKeeperFileState.date_basis,
            )
            .where(MemoryKeeperFileState.effective_capture_datetime.isnot(None))
            .where(active_file)
            .where(memorykeeper_link)
        )
        if filters.year is not None:
            statement = statement.where(
                MemoryKeeperFileState.effective_capture_year == filters.year
            )
        if filters.favorite is not None:
            statement = statement.where(
                MemoryKeeperFileState.favorite.is_(filters.favorite)
            )
        if filters.date_from is not None:
            statement = statement.where(
                MemoryKeeperFileState.effective_capture_date >= filters.date_from
            )
        if filters.date_to is not None:
            statement = statement.where(
                MemoryKeeperFileState.effective_capture_date <= filters.date_to
            )
        if cursor_datetime is not None and cursor_file_id is not None:
            statement = statement.where(
                or_(
                    MemoryKeeperFileState.effective_capture_datetime
                    < cursor_datetime,
                    and_(
                        MemoryKeeperFileState.effective_capture_datetime
                        == cursor_datetime,
                        MemoryKeeperFileState.file_id < cursor_file_id,
                    ),
                )
            )

        location_conditions = []
        if filters.country is not None:
            location_conditions.append(
                self._country_expression() == filters.country
            )
        if filters.region is not None:
            location_conditions.append(self._region_expression() == filters.region)
        if filters.place_id is not None:
            location_conditions.append(
                CommonFileMetadata.memorykeeper_place_id == filters.place_id
            )
        if location_conditions:
            location_match = self._correlated_exists(
                select(CommonFileMetadata.id)
                .select_from(CommonFileMetadata)
                .outerjoin(
                    MemoryKeeperPlace,
                    and_(
                        MemoryKeeperPlace.id
                        == CommonFileMetadata.memorykeeper_place_id,
                        MemoryKeeperPlace.deleted_at.is_(None),
                    ),
                )
                .where(
                    CommonFileMetadata.file_id == MemoryKeeperFileState.file_id,
                    *location_conditions,
                )
            )
            statement = statement.where(location_match)
        if filters.has_gps is not None:
            gps_match = self._correlated_exists(
                select(CommonFileMetadata.id).where(
                    CommonFileMetadata.file_id == MemoryKeeperFileState.file_id,
                    CommonFileMetadata.gps_lat.isnot(None),
                    CommonFileMetadata.gps_lon.isnot(None),
                )
            )
            statement = statement.where(
                gps_match if filters.has_gps else ~gps_match
            )

        return (
            statement.order_by(
                MemoryKeeperFileState.effective_capture_datetime.desc(),
                MemoryKeeperFileState.file_id.desc(),
            )
            .limit(limit + 1)
            .subquery("gallery_candidates")
        )

    def build_photos_statement(
        self,
        *,
        filters: FastGalleryFilters,
        limit: int,
        cursor_datetime: datetime | None,
        cursor_file_id: int | None,
    ) -> Select:
        """Build the exact statement used by `/photos` and EXPLAIN tests."""
        candidates = self._photo_candidates(
            filters=filters,
            limit=limit,
            cursor_datetime=cursor_datetime,
            cursor_file_id=cursor_file_id,
        )
        if self.db.get_bind().dialect.name == "postgresql":
            return self._postgresql_photos_statement(candidates)
        return self._portable_photos_statement(candidates)

    def _postgresql_photos_statement(self, candidates) -> Select:
        """Attach card fields through order-preserving point lookups.

        LATERAL plus ``OFFSET 0`` keeps each lookup dependent on the already
        ordered, limited candidate stream.  PostgreSQL can therefore retain
        the state index pathkeys through the Nested Loops instead of rebuilding
        the full JOIN result and sorting it.
        """
        file_lookup = (
            select(
                CommonFile.id.label("common_file_id"),
                CommonFile.file_id,
                CommonFile.original_name.label("filename"),
                CommonFile.preview_path,
                CommonFile.thumb_path,
            )
            .where(CommonFile.id == candidates.c.common_file_id)
            .offset(0)
            .lateral("gallery_file")
        )
        metadata_lookup = (
            select(
                CommonFileMetadata.memorykeeper_place_id,
                CommonFileMetadata.place_name,
                CommonFileMetadata.country,
                CommonFileMetadata.province,
                CommonFileMetadata.city,
                CommonFileMetadata.gps_lat,
                CommonFileMetadata.gps_lon,
            )
            .where(CommonFileMetadata.file_id == candidates.c.common_file_id)
            .offset(0)
            .lateral("gallery_metadata")
        )
        place_lookup = (
            select(
                MemoryKeeperPlace.display_name,
                MemoryKeeperPlace.country,
                MemoryKeeperPlace.province,
                MemoryKeeperPlace.city,
            )
            .where(
                MemoryKeeperPlace.id == metadata_lookup.c.memorykeeper_place_id,
                MemoryKeeperPlace.deleted_at.is_(None),
            )
            .offset(0)
            .lateral("gallery_place")
        )
        return self._photo_projection_statement(
            candidates=candidates,
            file_lookup=file_lookup,
            metadata_lookup=metadata_lookup,
            place_lookup=place_lookup,
            from_clause=(
                candidates.join(file_lookup, true())
                .outerjoin(metadata_lookup, true())
                .outerjoin(place_lookup, true())
            ),
        )

    def _portable_photos_statement(self, candidates) -> Select:
        """SQLite-compatible equivalent used by the existing unit suite."""
        file_lookup = select(
            CommonFile.id.label("common_file_id"),
            CommonFile.file_id,
            CommonFile.original_name.label("filename"),
            CommonFile.preview_path,
            CommonFile.thumb_path,
        ).subquery("gallery_file")
        metadata_lookup = CommonFileMetadata.__table__
        place_lookup = MemoryKeeperPlace.__table__
        return self._photo_projection_statement(
            candidates=candidates,
            file_lookup=file_lookup,
            metadata_lookup=metadata_lookup,
            place_lookup=place_lookup,
            from_clause=(
                candidates.join(
                    file_lookup,
                    file_lookup.c.common_file_id == candidates.c.common_file_id,
                )
                .outerjoin(
                    metadata_lookup,
                    metadata_lookup.c.file_id == candidates.c.common_file_id,
                )
                .outerjoin(
                    place_lookup,
                    and_(
                        place_lookup.c.id
                        == metadata_lookup.c.memorykeeper_place_id,
                        place_lookup.c.deleted_at.is_(None),
                    ),
                )
            ),
        )

    def _photo_projection_statement(
        self,
        *,
        candidates,
        file_lookup,
        metadata_lookup,
        place_lookup,
        from_clause,
    ) -> Select:
        has_gps = case(
            (
                and_(
                    metadata_lookup.c.gps_lat.isnot(None),
                    metadata_lookup.c.gps_lon.isnot(None),
                ),
                True,
            ),
            else_=False,
        ).label("has_gps")
        country = func.coalesce(
            place_lookup.c.country,
            metadata_lookup.c.country,
        ).label("country")
        region = func.coalesce(
            place_lookup.c.city,
            place_lookup.c.province,
            metadata_lookup.c.city,
            metadata_lookup.c.province,
        ).label("region")
        place_display_name = func.coalesce(
            place_lookup.c.display_name,
            metadata_lookup.c.place_name,
        ).label("place_display_name")
        return (
            select(
                candidates.c.common_file_id,
                file_lookup.c.file_id,
                file_lookup.c.filename,
                file_lookup.c.preview_path,
                file_lookup.c.thumb_path,
                candidates.c.favorite,
                has_gps,
                candidates.c.effective_capture_datetime,
                candidates.c.effective_capture_date,
                candidates.c.effective_capture_year,
                candidates.c.date_basis,
                metadata_lookup.c.memorykeeper_place_id,
                place_display_name,
                country,
                region,
            )
            .select_from(from_clause)
            .order_by(
                candidates.c.effective_capture_datetime.desc(),
                candidates.c.common_file_id.desc(),
            )
        )

    def photos(
        self,
        *,
        filters: FastGalleryFilters,
        limit: int,
        cursor_datetime: datetime | None,
        cursor_file_id: int | None,
    ) -> list[object]:
        statement = self.build_photos_statement(
            filters=filters,
            limit=limit,
            cursor_datetime=cursor_datetime,
            cursor_file_id=cursor_file_id,
        )
        return list(self.db.execute(statement).all())

    def summary(self) -> dict[str, object]:
        has_gps = self._has_gps_expression()
        overall = self._base_query().with_entities(
            func.count(CommonFile.id),
            func.coalesce(
                func.sum(case((MemoryKeeperFileState.favorite.is_(True), 1), else_=0)),
                0,
            ),
            func.coalesce(func.sum(case((has_gps.is_(True), 1), else_=0)), 0),
            func.min(MemoryKeeperFileState.effective_capture_date),
            func.max(MemoryKeeperFileState.effective_capture_date),
        ).one()
        year_rows = (
            self._base_query()
            .with_entities(
                MemoryKeeperFileState.effective_capture_year,
                func.count(CommonFile.id),
            )
            .group_by(MemoryKeeperFileState.effective_capture_year)
            .order_by(MemoryKeeperFileState.effective_capture_year.desc())
            .all()
        )
        country = self._country_expression()
        country_rows = (
            self._base_query()
            .with_entities(country.label("country"), func.count(CommonFile.id))
            .group_by(country)
            .order_by(country.asc().nulls_last())
            .all()
        )
        return {
            "total_photos": int(overall[0] or 0),
            "favorite_count": int(overall[1] or 0),
            "gps_count": int(overall[2] or 0),
            "effective_date_min": overall[3],
            "effective_date_max": overall[4],
            "by_year": [(int(year), int(count)) for year, count in year_rows],
            "by_country": [(country_name, int(count)) for country_name, count in country_rows],
        }

    def hierarchy(self) -> list[object]:
        country = self._country_expression()
        region = self._region_expression()
        place_display_name = self._place_display_expression()
        return (
            self._base_query()
            .with_entities(
                MemoryKeeperFileState.effective_capture_year.label("year"),
                country.label("country"),
                region.label("region"),
                CommonFileMetadata.memorykeeper_place_id,
                place_display_name.label("place_display_name"),
                func.count(CommonFile.id).label("count"),
            )
            .group_by(
                MemoryKeeperFileState.effective_capture_year,
                country,
                region,
                CommonFileMetadata.memorykeeper_place_id,
                place_display_name,
            )
            .order_by(
                MemoryKeeperFileState.effective_capture_year.desc(),
                country.asc().nulls_last(),
                region.asc().nulls_last(),
                place_display_name.asc().nulls_last(),
                CommonFileMetadata.memorykeeper_place_id.asc().nulls_last(),
            )
            .all()
        )
