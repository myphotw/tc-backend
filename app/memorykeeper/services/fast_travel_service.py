"""MemoryKeeper TravelRecords fast-read service."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.common.services.gallery_media import build_gallery_media_url
from app.memorykeeper.repositories.fast_travel_repository import (
    MemoryKeeperFastTravelRepository,
)
from app.memorykeeper.schemas.fast_travel import (
    FastTravelAggregatesResponse,
    FastTravelCountryAggregate,
    FastTravelMemoriesResponse,
    FastTravelMemoryCandidate,
    FastTravelPlaceAggregate,
)


class MemoryKeeperFastTravelService:
    """Build TravelRecords projections from canonical capture calendar dates."""

    PREVIOUS_YEAR_WINDOW_DAYS = 7
    EXACT_ANNIVERSARY = "EXACT_ANNIVERSARY"
    PREVIOUS_YEAR_PERIOD = "PREVIOUS_YEAR_PERIOD"
    PAST_YEAR_PERIOD = "PAST_YEAR_PERIOD"
    LONG_AGO = "LONG_AGO"

    def __init__(self, db: Session) -> None:
        self.repository = MemoryKeeperFastTravelRepository(db)

    def aggregates(self) -> FastTravelAggregatesResponse:
        place_buckets: dict[tuple[object, ...], dict[str, object]] = {}
        for row in self.repository.place_date_rows():
            key = self._place_key(row)
            bucket = place_buckets.setdefault(
                key,
                {
                    "photo_count": 0,
                    "capture_dates": [],
                    "representative": None,
                    "latitude": None,
                    "longitude": None,
                },
            )
            bucket["photo_count"] = int(bucket["photo_count"]) + int(
                row.photo_count
            )
            bucket["capture_dates"].append(row.effective_capture_date)  # type: ignore[union-attr]
            if row.representative_common_file_id is not None:
                bucket["representative"] = self._to_aggregate_representative(row)
            if row.latitude is not None and row.longitude is not None:
                bucket["latitude"] = float(row.latitude)
                bucket["longitude"] = float(row.longitude)

        country_buckets: dict[object, dict[str, object]] = {}
        for row in self.repository.country_date_rows():
            bucket = country_buckets.setdefault(
                row.country,
                {
                    "photo_count": 0,
                    "capture_dates": [],
                    "representative": None,
                },
            )
            bucket["photo_count"] = int(bucket["photo_count"]) + int(
                row.photo_count
            )
            bucket["capture_dates"].append(row.effective_capture_date)  # type: ignore[union-attr]
            if row.representative_common_file_id is not None:
                bucket["representative"] = self._to_aggregate_representative(row)

        places = [
            FastTravelPlaceAggregate(
                memorykeeper_place_id=key[0],
                place_display_name=key[1],
                country=key[2],
                region=key[3],
                latitude=bucket["latitude"],
                longitude=bucket["longitude"],
                photo_count=int(bucket["photo_count"]),
                capture_dates=list(bucket["capture_dates"]),
                visit_count=self.count_consecutive_date_ranges(
                    bucket["capture_dates"]  # type: ignore[arg-type]
                ),
                **self._representative_fields(
                    bucket["representative"]  # type: ignore[arg-type]
                ),
            )
            for key, bucket in place_buckets.items()
        ]
        countries = [
            FastTravelCountryAggregate(
                country=country,
                photo_count=int(bucket["photo_count"]),
                capture_dates=list(bucket["capture_dates"]),
                visit_count=self.count_consecutive_date_ranges(
                    bucket["capture_dates"]  # type: ignore[arg-type]
                ),
                **self._representative_fields(
                    bucket["representative"]  # type: ignore[arg-type]
                ),
            )
            for country, bucket in country_buckets.items()
        ]
        return FastTravelAggregatesResponse(places=places, countries=countries)

    def memories(
        self,
        *,
        reference_date: date,
        limit: int,
    ) -> FastTravelMemoriesResponse:
        exact_rows = self.repository.exact_anniversary_candidates(reference_date)
        previous_anchor = self._previous_year_date(reference_date)
        previous_rows = self.repository.previous_year_period_candidates(
            date_from=previous_anchor - timedelta(days=self.PREVIOUS_YEAR_WINDOW_DAYS),
            date_to=previous_anchor + timedelta(days=self.PREVIOUS_YEAR_WINDOW_DAYS),
            reference_date=reference_date,
        )
        period_from = reference_date - timedelta(days=self.PREVIOUS_YEAR_WINDOW_DAYS)
        period_to = reference_date + timedelta(days=self.PREVIOUS_YEAR_WINDOW_DAYS)
        past_rows = self.repository.past_year_period_candidates(
            reference_date=reference_date,
            period_from=period_from,
            period_to=period_to,
            limit=max(limit * 4, limit),
        )
        long_ago_rows = self.repository.long_ago_candidates(
            reference_date=reference_date,
            limit=max(limit * 8, limit),
        )

        exact = [
            self._to_memory_candidate(
                row,
                reference_date=reference_date,
                previous_anchor=previous_anchor,
                exact_anniversary=True,
                category=self.EXACT_ANNIVERSARY,
            )
            for row in exact_rows
        ]
        exact_dates = {item.effective_capture_date for item in exact}
        previous = [
            self._to_memory_candidate(
                row,
                reference_date=reference_date,
                previous_anchor=previous_anchor,
                exact_anniversary=False,
                category=self.PREVIOUS_YEAR_PERIOD,
            )
            for row in previous_rows
            if row.effective_capture_date not in exact_dates
        ]
        previous.sort(
            key=lambda item: (
                abs(item.day_offset),
                -item.effective_capture_date.toordinal(),
                -item.common_file_id,
            )
        )

        used_file_ids = {
            item.common_file_id for item in [*exact, *previous]
        }
        past_year_period = []
        for row in past_rows:
            common_file_id = int(row.common_file_id)
            if common_file_id in used_file_ids:
                continue
            item = self._to_memory_candidate(
                row,
                reference_date=reference_date,
                previous_anchor=previous_anchor,
                exact_anniversary=False,
                category=self.PAST_YEAR_PERIOD,
            )
            past_year_period.append(item)
            used_file_ids.add(common_file_id)
        past_year_period.sort(
            key=lambda item: (
                abs(item.day_offset),
                -item.effective_capture_date.toordinal(),
                -item.common_file_id,
            )
        )

        long_ago = []
        for row in long_ago_rows:
            common_file_id = int(row.common_file_id)
            if common_file_id in used_file_ids:
                continue
            item = self._to_memory_candidate(
                row,
                reference_date=reference_date,
                previous_anchor=previous_anchor,
                exact_anniversary=False,
                category=self.LONG_AGO,
            )
            long_ago.append(item)
            used_file_ids.add(common_file_id)

        legacy_exact = exact[:limit]
        legacy_previous = previous[: max(limit - len(legacy_exact), 0)]
        bounded_past = past_year_period[:limit]
        bounded_long_ago = long_ago[:limit]
        return FastTravelMemoriesResponse(
            reference_date=reference_date,
            items=self._diverse_items(
                [exact, previous, past_year_period, long_ago],
                limit=limit,
            ),
            exact_anniversary=legacy_exact,
            previous_year_period=legacy_previous,
            past_year_period=bounded_past,
            long_ago=bounded_long_ago,
        )

    @staticmethod
    def count_consecutive_date_ranges(values: list[date]) -> int:
        ordered = sorted(set(values))
        if not ordered:
            return 0
        return 1 + sum(
            current != previous + timedelta(days=1)
            for previous, current in zip(ordered, ordered[1:])
        )

    @staticmethod
    def _place_key(row: object) -> tuple[object, ...]:
        return (
            row.memorykeeper_place_id,  # type: ignore[attr-defined]
            row.place_display_name,  # type: ignore[attr-defined]
            row.country,  # type: ignore[attr-defined]
            row.region,  # type: ignore[attr-defined]
        )

    @staticmethod
    def _to_aggregate_representative(row: object) -> dict[str, object]:
        public_file_id = str(row.representative_file_id)  # type: ignore[attr-defined]
        return {
            "common_file_id": int(row.representative_common_file_id),  # type: ignore[attr-defined]
            "file_id": public_file_id,
            "capture_date": row.representative_capture_date,  # type: ignore[attr-defined]
            "preview_url": build_gallery_media_url(
                public_file_id,
                "preview",
                row.representative_preview_path,  # type: ignore[attr-defined]
            ),
            "thumbnail_url": build_gallery_media_url(
                public_file_id,
                "thumbnail",
                row.representative_thumb_path,  # type: ignore[attr-defined]
            ),
        }

    @staticmethod
    def _representative_fields(
        representative: dict[str, object] | None,
    ) -> dict[str, object]:
        if representative is None:
            return {}
        return {
            "representative_common_file_id": representative["common_file_id"],
            "representative_file_id": representative["file_id"],
            "representative_capture_date": representative["capture_date"],
            "representative_preview_url": representative["preview_url"],
            "representative_thumbnail_url": representative["thumbnail_url"],
        }

    def _to_memory_candidate(
        self,
        row: object,
        *,
        reference_date: date,
        previous_anchor: date,
        exact_anniversary: bool,
        category: str,
    ) -> FastTravelMemoryCandidate:
        public_file_id = str(row.file_id)  # type: ignore[attr-defined]
        captured_date = row.effective_capture_date  # type: ignore[attr-defined]
        if exact_anniversary:
            day_offset = 0
        elif category == self.PREVIOUS_YEAR_PERIOD:
            day_offset = (captured_date - previous_anchor).days
        elif category == self.PAST_YEAR_PERIOD:
            period_anchor = self._date_in_year(reference_date, captured_date.year)
            day_offset = (captured_date - period_anchor).days
        else:
            day_offset = (captured_date - reference_date).days
        return FastTravelMemoryCandidate(
            common_file_id=int(row.common_file_id),  # type: ignore[attr-defined]
            file_id=public_file_id,
            effective_capture_date=captured_date,
            effective_capture_year=captured_date.year,
            years_ago=max(reference_date.year - captured_date.year, 0),
            day_offset=day_offset,
            memorykeeper_place_id=row.memorykeeper_place_id,  # type: ignore[attr-defined]
            place_display_name=row.place_display_name,  # type: ignore[attr-defined]
            country=row.country,  # type: ignore[attr-defined]
            preview_url=build_gallery_media_url(
                public_file_id,
                "preview",
                row.preview_path,  # type: ignore[attr-defined]
            ),
            thumbnail_url=build_gallery_media_url(
                public_file_id,
                "thumbnail",
                row.thumb_path,  # type: ignore[attr-defined]
            ),
            category=category,
        )

    @staticmethod
    def _diverse_items(
        categories: list[list[FastTravelMemoryCandidate]],
        *,
        limit: int,
    ) -> list[FastTravelMemoryCandidate]:
        """Round-robin bounded categories without duplicating a CommonFile."""
        result: list[FastTravelMemoryCandidate] = []
        seen: set[int] = set()
        indexes = [0 for _ in categories]
        while len(result) < limit:
            progressed = False
            for category_index, candidates in enumerate(categories):
                while indexes[category_index] < len(candidates):
                    candidate = candidates[indexes[category_index]]
                    indexes[category_index] += 1
                    if candidate.common_file_id in seen:
                        continue
                    result.append(candidate)
                    seen.add(candidate.common_file_id)
                    progressed = True
                    break
                if len(result) >= limit:
                    break
            if not progressed:
                break
        return result

    @staticmethod
    def _date_in_year(reference_date: date, year: int) -> date:
        last_day = calendar.monthrange(year, reference_date.month)[1]
        return date(
            year,
            reference_date.month,
            min(reference_date.day, last_day),
        )

    @staticmethod
    def _previous_year_date(reference_date: date) -> date:
        return MemoryKeeperFastTravelService._date_in_year(
            reference_date,
            reference_date.year - 1,
        )
