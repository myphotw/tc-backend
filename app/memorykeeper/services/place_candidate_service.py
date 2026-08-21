from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from math import log10
import re
from typing import Callable

from sqlalchemy.orm import Session

from app.common.models.file_metadata import CommonFileMetadata
from app.common.services.api_clients.base_client import ApiClientError
from app.common.services.api_clients.google import PlacesClient
from app.common.services.key_resolver import ExternalServiceName, KeyResolver
from app.memorykeeper.services.place_matcher import MemoryKeeperPlaceMatcher

logger = logging.getLogger(__name__)


class PlaceCreationSource:
    USER = "USER"
    MIGRATION = "MIGRATION"
    AUTO_POI = "AUTO_POI"
    AUTO_LOCALITY = "AUTO_LOCALITY"
    AUTO_ADDRESS = "AUTO_ADDRESS"
    AUTO_GPS = "AUTO_GPS"


@dataclass(frozen=True)
class AutoPlaceCandidate:
    display_name: str
    canonical_name: str
    address: str | None
    latitude: float
    longitude: float
    radius_m: float
    provider_place_id: str | None
    category: str
    creation_source: str
    provider_types: tuple[str, ...] = ()
    score: float | None = None
    country: str | None = None
    province: str | None = None
    city: str | None = None
    district: str | None = None


NearbyLookup = Callable[[float, float, int], list[dict[str, object]]]


class MemoryKeeperPlaceCandidateService:
    """Select a memorable POI, then fall back conservatively to raw metadata."""

    SEARCH_RADIUS_M = 1500
    DEFAULT_RADIUS_M = 200.0
    MIN_POI_SCORE = 45.0

    TYPE_SCORES: dict[str, float] = {
        "natural_feature": 120,
        "tourist_attraction": 115,
        "park": 110,
        "campground": 105,
        "museum": 100,
        "amusement_park": 100,
        "aquarium": 95,
        "zoo": 95,
        "art_gallery": 90,
        "lodging": 75,
        "restaurant": 65,
        "cafe": 60,
        "bakery": 55,
        "bar": 50,
        "point_of_interest": 25,
        "establishment": 15,
    }

    CATEGORY_BY_TYPE: tuple[tuple[set[str], str], ...] = (
        ({"natural_feature", "park"}, "NATURE"),
        ({"tourist_attraction", "amusement_park", "aquarium", "zoo"}, "ATTRACTION"),
        ({"campground"}, "CAMPGROUND"),
        ({"museum", "art_gallery"}, "CULTURE"),
        ({"lodging"}, "LODGING"),
        ({"restaurant", "cafe", "bakery", "bar"}, "FOOD"),
    )

    _COORDINATE_NAME = re.compile(r"^\s*-?\d+(?:\.\d+)?\s*[,/]\s*-?\d+(?:\.\d+)?\s*$")
    _ADDRESS_LIKE = re.compile(r"(?:\d{1,5}(?:-\d{1,5})?|\b(?:road|street|st\.?|ave\.?|번지)\b)", re.IGNORECASE)

    def __init__(
        self,
        db: Session,
        *,
        nearby_lookup: NearbyLookup | None = None,
        record_usage: bool = True,
    ) -> None:
        self.db = db
        self.nearby_lookup = nearby_lookup
        self.record_usage = record_usage

    def choose(self, metadata: CommonFileMetadata) -> AutoPlaceCandidate:
        latitude = float(metadata.gps_lat)
        longitude = float(metadata.gps_lon)
        nearby = self._load_nearby(latitude, longitude)
        poi = self.choose_poi(
            nearby,
            latitude=latitude,
            longitude=longitude,
            raw_address=metadata.place_name,
        )
        if poi is not None:
            return replace(
                poi,
                country=self._clean(metadata.country),
                province=self._clean(metadata.province),
                city=self._clean(metadata.city),
                district=self._clean(metadata.district),
            )

        locality = self._meaningful_locality(metadata)
        if locality:
            return self._fallback(
                locality,
                metadata=metadata,
                source=PlaceCreationSource.AUTO_LOCALITY,
                category="LOCALITY",
            )
        raw_address = self._clean(metadata.place_name)
        if raw_address:
            return self._fallback(
                raw_address,
                metadata=metadata,
                source=PlaceCreationSource.AUTO_ADDRESS,
                category="ADDRESS",
            )
        gps_name = f"GPS {latitude:.6f}, {longitude:.6f}"
        return self._fallback(
            gps_name,
            metadata=metadata,
            source=PlaceCreationSource.AUTO_GPS,
            category="GPS",
        )

    def choose_poi(
        self,
        items: list[dict[str, object]],
        *,
        latitude: float,
        longitude: float,
        raw_address: str | None,
    ) -> AutoPlaceCandidate | None:
        ranked: list[tuple[float, float, str, int, AutoPlaceCandidate]] = []
        for index, item in enumerate(items):
            candidate = self._rank_one(
                item,
                latitude=latitude,
                longitude=longitude,
                raw_address=raw_address,
            )
            if candidate is None or candidate.score is None:
                continue
            distance = MemoryKeeperPlaceMatcher.distance_m(
                latitude,
                longitude,
                candidate.latitude,
                candidate.longitude,
            )
            ranked.append(
                (
                    -candidate.score,
                    distance,
                    candidate.provider_place_id or candidate.canonical_name.casefold(),
                    index,
                    candidate,
                )
            )
        return min(ranked)[-1] if ranked else None

    def _rank_one(
        self,
        item: dict[str, object],
        *,
        latitude: float,
        longitude: float,
        raw_address: str | None,
    ) -> AutoPlaceCandidate | None:
        name = self._clean(item.get("place_name") or item.get("name"))
        if not name or self._COORDINATE_NAME.match(name):
            return None
        try:
            place_lat = float(item["latitude"])
            place_lon = float(item["longitude"])
        except (KeyError, TypeError, ValueError):
            return None
        distance = MemoryKeeperPlaceMatcher.distance_m(
            latitude,
            longitude,
            place_lat,
            place_lon,
        )
        if distance > self.SEARCH_RADIUS_M:
            return None
        types = tuple(str(value) for value in item.get("types") or ())
        type_score = max((self.TYPE_SCORES.get(value, 0) for value in types), default=0)
        if type_score <= 0:
            return None
        if (
            type_score < 50
            and self._ADDRESS_LIKE.search(name)
        ):
            return None
        if str(item.get("business_status") or "") == "CLOSED_PERMANENTLY":
            return None
        score = type_score - min(distance / 50.0, 30.0)
        rating = self._float(item.get("rating"))
        rating_count = self._float(item.get("user_ratings_total"))
        if rating is not None:
            score += max(0.0, min(rating, 5.0))
        if rating_count is not None and rating_count > 0:
            score += min(log10(rating_count + 1) * 4.0, 12.0)
        if str(item.get("business_status") or "") == "CLOSED_TEMPORARILY":
            score -= 15.0
        if score < self.MIN_POI_SCORE:
            return None
        return AutoPlaceCandidate(
            display_name=name,
            canonical_name=name,
            address=self._clean(raw_address),
            latitude=place_lat,
            longitude=place_lon,
            radius_m=self.DEFAULT_RADIUS_M,
            provider_place_id=self._clean(item.get("place_id")),
            category=self._category(types),
            creation_source=PlaceCreationSource.AUTO_POI,
            provider_types=types,
            score=score,
        )

    def _load_nearby(self, latitude: float, longitude: float) -> list[dict[str, object]]:
        try:
            if self.nearby_lookup is not None:
                return self.nearby_lookup(latitude, longitude, self.SEARCH_RADIUS_M)
            api_key = KeyResolver(self.db).resolve(ExternalServiceName.GOOGLE_PLACES)
            if not api_key:
                return []
            return PlacesClient(
                api_key=api_key,
                db=self.db if self.record_usage else None,
            ).nearby(
                latitude=latitude,
                longitude=longitude,
                radius_m=self.SEARCH_RADIUS_M,
            )
        except ApiClientError as exc:
            logger.warning(
                "MemoryKeeper nearby candidate lookup failed code=%s",
                exc.code,
            )
            return []

    def _fallback(
        self,
        name: str,
        *,
        metadata: CommonFileMetadata,
        source: str,
        category: str,
    ) -> AutoPlaceCandidate:
        return AutoPlaceCandidate(
            display_name=name,
            canonical_name=name,
            address=self._clean(metadata.place_name),
            latitude=float(metadata.gps_lat),
            longitude=float(metadata.gps_lon),
            radius_m=self.DEFAULT_RADIUS_M,
            provider_place_id=None,
            category=category,
            creation_source=source,
            country=self._clean(metadata.country),
            province=self._clean(metadata.province),
            city=self._clean(metadata.city),
            district=self._clean(metadata.district),
        )

    @staticmethod
    def _meaningful_locality(metadata: CommonFileMetadata) -> str | None:
        for value in (metadata.district, metadata.city, metadata.province):
            cleaned = MemoryKeeperPlaceCandidateService._clean(value)
            if cleaned:
                return cleaned
        return None

    @classmethod
    def _category(cls, types: tuple[str, ...]) -> str:
        values = set(types)
        for mapped_types, category in cls.CATEGORY_BY_TYPE:
            if values & mapped_types:
                return category
        return "PLACE"

    @staticmethod
    def _clean(value: object | None) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @staticmethod
    def _float(value: object | None) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
