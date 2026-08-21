from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

from sqlalchemy.orm import Session

from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.repositories.place_repository import MemoryKeeperPlaceRepository


class PlaceMatchSource:
    PROVIDER_PLACE_ID = "PROVIDER_PLACE_ID"
    CANONICAL_NAME = "CANONICAL_NAME"
    RADIUS = "RADIUS"
    NONE = "NONE"
    USER = "USER"
    AUTO_PLACE_MATCH = "AUTO_PLACE_MATCH"
    PLACE_DELETED = "PLACE_DELETED"


@dataclass(frozen=True)
class PlaceMatch:
    place: MemoryKeeperPlace | None
    distance_m: float | None
    source: str

    @property
    def matched(self) -> bool:
        return self.place is not None


class MemoryKeeperPlaceMatcher:
    """Deterministic MemoryKeeper-only representative-place matcher."""

    EARTH_RADIUS_M = 6_371_000.0

    def __init__(self, db: Session) -> None:
        self.repository = MemoryKeeperPlaceRepository(db)

    def match(
        self,
        *,
        gps_lat: float,
        gps_lon: float,
        provider_place_id: str | None = None,
        canonical_name: str | None = None,
    ) -> PlaceMatch:
        places = self.repository.active_places()
        provider = self._clean(provider_place_id)
        if provider:
            matches = [p for p in places if self._clean(p.provider_place_id) == provider]
            if matches:
                place = min(matches, key=lambda item: item.id)
                return PlaceMatch(place, self.distance_m(gps_lat, gps_lon, place.latitude, place.longitude), PlaceMatchSource.PROVIDER_PLACE_ID)

        canonical = self._clean(canonical_name, casefold=True)
        if canonical:
            matches = [p for p in places if self._clean(p.canonical_name, casefold=True) == canonical]
            if matches:
                place = min(matches, key=lambda item: item.id)
                return PlaceMatch(place, self.distance_m(gps_lat, gps_lon, place.latitude, place.longitude), PlaceMatchSource.CANONICAL_NAME)

        candidates: list[tuple[float, str, MemoryKeeperPlace]] = []
        for place in places:
            distance = self.distance_m(gps_lat, gps_lon, place.latitude, place.longitude)
            if distance <= float(place.radius_m):
                candidates.append((distance, place.id, place))
        if candidates:
            distance, _, place = min(candidates, key=lambda item: (item[0], item[1]))
            return PlaceMatch(place, distance, PlaceMatchSource.RADIUS)
        return PlaceMatch(None, None, PlaceMatchSource.NONE)

    @classmethod
    def distance_m(cls, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        lat1r, lon1r, lat2r, lon2r = map(radians, (lat1, lon1, lat2, lon2))
        dlat = lat2r - lat1r
        dlon = lon2r - lon1r
        value = sin(dlat / 2) ** 2 + cos(lat1r) * cos(lat2r) * sin(dlon / 2) ** 2
        return 2 * cls.EARTH_RADIUS_M * asin(sqrt(value))

    @staticmethod
    def _clean(value: str | None, *, casefold: bool = False) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        return cleaned.casefold() if casefold else cleaned
