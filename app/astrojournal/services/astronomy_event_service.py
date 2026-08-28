"""AstroJournal astronomy event normalization and cache policy."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import re
from threading import RLock
from typing import Callable

from app.astrojournal.schemas.astronomy_event import (
    AstronomyEventItem,
    AstronomyEventListResponse,
    AstronomyEventType,
)
from app.common.services.api_clients.base_client import (
    ApiClientError,
    ExternalApiErrorCode,
)
from app.common.services.api_clients.spacecatalog import SpaceCatalogEventsClient


@dataclass(frozen=True)
class _CacheKey:
    from_at: datetime
    to_at: datetime


@dataclass(frozen=True)
class _CacheEntry:
    events: tuple[AstronomyEventItem, ...]
    fetched_at: datetime


class AstronomyEventMemoryCache:
    """Process-local cache retaining expired entries for stale fallback."""

    def __init__(self) -> None:
        self._entries: dict[_CacheKey, _CacheEntry] = {}
        self._lock = RLock()

    def get(self, key: _CacheKey) -> _CacheEntry | None:
        with self._lock:
            return self._entries.get(key)

    def put(self, key: _CacheKey, entry: _CacheEntry) -> None:
        with self._lock:
            self._entries[key] = entry

    def latest(self) -> _CacheEntry | None:
        with self._lock:
            if not self._entries:
                return None
            return max(self._entries.values(), key=lambda entry: entry.fetched_at)


_DEFAULT_CACHE = AstronomyEventMemoryCache()


class AstronomyEventService:
    CACHE_TTL = timedelta(hours=24)
    MIN_MAJOR_SHOWER_ZHR = 10.0

    _PLANET_NAMES = {
        "mercury": "수성",
        "venus": "금성",
        "moon": "달",
        "mars": "화성",
        "jupiter": "목성",
        "saturn": "토성",
        "uranus": "천왕성",
        "neptune": "해왕성",
    }
    _OPPOSITION_OBJECTS = {"mars", "jupiter", "saturn", "uranus", "neptune"}
    _ELONGATION_OBJECTS = {"mercury", "venus"}
    _CONJUNCTION_OBJECTS = {
        "moon",
        "mercury",
        "venus",
        "mars",
        "jupiter",
        "saturn",
        "uranus",
        "neptune",
    }
    _SHOWER_NAMES = {
        "draconids": "용자리 유성우",
        "southern-taurids": "남쪽 황소자리 유성우",
        "orionids": "오리온자리 유성우",
        "northern-taurids": "북쪽 황소자리 유성우",
        "leonids": "사자자리 유성우",
        "geminids": "쌍둥이자리 유성우",
        "ursids": "작은곰자리 유성우",
        "quadrantids": "사분의자리 유성우",
        "lyrids": "거문고자리 유성우",
        "eta-aquariids": "물병자리 에타 유성우",
        "southern-delta-aquariids": "남쪽 물병자리 델타 유성우",
        "alpha-capricornids": "염소자리 알파 유성우",
        "perseids": "페르세우스자리 유성우",
    }
    _ECLIPSE_TITLES = {
        "total solar eclipse": "개기일식",
        "annular solar eclipse": "금환일식",
        "partial solar eclipse": "부분일식",
        "total lunar eclipse": "개기월식",
        "partial lunar eclipse": "부분월식",
        "penumbral lunar eclipse": "반영월식",
    }
    _TAGS = {
        "meteor_shower": ["맨눈 관측", "광시야 촬영"],
        "solar_eclipse": ["보호장비 필수", "촬영 추천"],
        "lunar_eclipse": ["맨눈 관측", "촬영 추천"],
        "planet_viewing": ["망원경 관측", "행성 촬영"],
        "conjunction": ["맨눈 관측", "촬영 추천"],
    }
    _PRIORITY = {
        "solar_eclipse": 100,
        "lunar_eclipse": 95,
        "meteor_shower": 90,
        "planet_viewing": 80,
        "conjunction": 70,
    }

    def __init__(
        self,
        *,
        client: SpaceCatalogEventsClient | None = None,
        cache: AstronomyEventMemoryCache | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client or SpaceCatalogEventsClient()
        self.cache = cache or _DEFAULT_CACHE
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def list_events(
        self,
        *,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
    ) -> AstronomyEventListResponse:
        now = self._aware_utc(self.clock(), name="current time")
        uses_default_range = from_at is None and to_at is None
        query_from = (
            self._aware_utc(from_at, name="from")
            if from_at is not None
            else datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
        )
        query_to = (
            self._aware_utc(to_at, name="to")
            if to_at is not None
            else self._add_months(query_from, 6)
        )
        self._validate_range(query_from, query_to)

        key = _CacheKey(from_at=query_from, to_at=query_to)
        cached = self.cache.get(key)
        if cached is not None and now - cached.fetched_at <= self.CACHE_TTL:
            return self._response(
                cached.events,
                query_from=query_from,
                query_to=query_to,
                now=now,
            )

        try:
            raw_events = self.client.list_events(
                from_at=query_from,
                to_at=query_to,
            )
            normalized = self._normalize_events(raw_events)
            self.cache.put(
                key,
                _CacheEntry(events=tuple(normalized), fetched_at=now),
            )
        except ApiClientError:
            fallback = cached or (self.cache.latest() if uses_default_range else None)
            if fallback is None:
                raise
            normalized = list(fallback.events)

        return self._response(
            normalized,
            query_from=query_from,
            query_to=query_to,
            now=now,
        )

    def _response(
        self,
        events: list[AstronomyEventItem] | tuple[AstronomyEventItem, ...],
        *,
        query_from: datetime,
        query_to: datetime,
        now: datetime,
    ) -> AstronomyEventListResponse:
        lower_bound = max(query_from, now)
        active = [
            event
            for event in events
            if (event.end_at or event.peak_at) >= lower_bound
            and event.peak_at <= query_to
        ]
        active.sort(key=lambda event: (event.peak_at, -event.priority, event.id))
        return AstronomyEventListResponse(events=active)

    def _normalize_events(
        self,
        raw_events: list[dict[str, object]],
    ) -> list[AstronomyEventItem]:
        unique: dict[str, AstronomyEventItem] = {}
        for raw in raw_events:
            event = self._normalize_event(raw)
            if event is not None and event.id not in unique:
                unique[event.id] = event
        return list(unique.values())

    def _normalize_event(
        self,
        raw: dict[str, object],
    ) -> AstronomyEventItem | None:
        event_id = raw.get("id")
        kind = raw.get("kind")
        if not isinstance(event_id, str) or not isinstance(kind, str):
            return None
        peak_at = self._provider_datetime(raw.get("time"))
        if peak_at is None:
            return None

        event_type: AstronomyEventType
        title: str | None
        start_at = None
        end_at = None
        if kind == "shower":
            circumstances = raw.get("circumstances")
            if not isinstance(circumstances, dict):
                return None
            zhr = self._number(circumstances.get("zhr"))
            if zhr is None or zhr < self.MIN_MAJOR_SHOWER_ZHR:
                return None
            event_type = "meteor_shower"
            title = self._shower_title(event_id)
            window = raw.get("window")
            if not isinstance(window, dict):
                return None
            start_at = self._provider_datetime(window.get("start"))
            end_at = self._provider_datetime(window.get("end"))
            if start_at is None or end_at is None:
                return None
        elif kind == "eclipse":
            provider_title = raw.get("title")
            if not isinstance(provider_title, str):
                return None
            title = self._ECLIPSE_TITLES.get(provider_title.casefold())
            if "solar" in provider_title.casefold():
                event_type = "solar_eclipse"
            elif "lunar" in provider_title.casefold():
                event_type = "lunar_eclipse"
            else:
                return None
        elif kind in {"opposition", "elongation"}:
            objects = self._objects(raw)
            allowed = (
                self._OPPOSITION_OBJECTS
                if kind == "opposition"
                else self._ELONGATION_OBJECTS
            )
            if len(objects) != 1 or objects[0] not in allowed:
                return None
            event_type = "planet_viewing"
            title = f"{self._PLANET_NAMES[objects[0]]} 관측 최적기"
        elif kind == "conjunction":
            objects = self._objects(raw)
            if len(objects) != 2 or any(
                item not in self._CONJUNCTION_OBJECTS for item in objects
            ):
                return None
            event_type = "conjunction"
            title = "·".join(self._PLANET_NAMES[item] for item in objects) + " 근접"
        else:
            return None

        if title is None:
            return None
        return AstronomyEventItem(
            id=event_id,
            type=event_type,
            title=title,
            start_at=start_at,
            peak_at=peak_at,
            end_at=end_at,
            tags=list(self._TAGS[event_type]),
            priority=self._PRIORITY[event_type],
        )

    @classmethod
    def _shower_title(cls, event_id: str) -> str | None:
        match = re.fullmatch(r"shower-(.+)-\d{4}-\d{2}-\d{2}", event_id)
        if match is None:
            return None
        return cls._SHOWER_NAMES.get(match.group(1))

    @staticmethod
    def _objects(raw: dict[str, object]) -> list[str]:
        objects = raw.get("objects")
        if not isinstance(objects, list) or not all(
            isinstance(item, str) for item in objects
        ):
            return []
        return objects

    @staticmethod
    def _number(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    @staticmethod
    def _provider_datetime(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _aware_utc(value: datetime, *, name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ApiClientError(
                f"{name} must include a timezone",
                code=ExternalApiErrorCode.INVALID_REQUEST,
            )
        return value.astimezone(timezone.utc)

    @classmethod
    def _validate_range(cls, from_at: datetime, to_at: datetime) -> None:
        if to_at <= from_at:
            raise ApiClientError(
                "to must be later than from",
                code=ExternalApiErrorCode.INVALID_REQUEST,
            )
        if to_at > cls._add_years(from_at, 2):
            raise ApiClientError(
                "event range cannot exceed two years",
                code=ExternalApiErrorCode.INVALID_REQUEST,
            )

    @staticmethod
    def _add_months(value: datetime, months: int) -> datetime:
        month_index = value.month - 1 + months
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        day = min(value.day, calendar.monthrange(year, month)[1])
        return value.replace(year=year, month=month, day=day)

    @staticmethod
    def _add_years(value: datetime, years: int) -> datetime:
        try:
            return value.replace(year=value.year + years)
        except ValueError:
            return value.replace(year=value.year + years, day=28)
