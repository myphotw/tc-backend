"""Application services for normalized external API access."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.common.repositories.geocode_cache_repository import GeocodeCacheRepository
from app.common.services.api_clients.base_client import (
    ApiClientError,
    ExternalApiErrorCode,
)
from app.common.services.api_clients.google import GeocodingClient, PlacesClient
from app.common.services.api_clients.weather import WeatherClient
from app.common.services.key_resolver import ExternalServiceName, KeyResolver


class ExternalApiService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.keys = KeyResolver(db)

    def reverse_geocode(
        self,
        *,
        latitude: float,
        longitude: float,
        language: str,
    ) -> dict[str, object]:
        cache = GeocodeCacheRepository(self.db)
        cached = cache.find(latitude=latitude, longitude=longitude)
        if cached is not None:
            return {
                "display_name": cached.place_name,
                "latitude": cached.latitude,
                "longitude": cached.longitude,
                "country": cached.country,
                "province": cached.province,
                "city": cached.city,
                "district": cached.district,
                "place_name": cached.place_name,
                "provider": cached.provider.lower(),
                "source": "cache",
            }

        api_key = self._require_key(ExternalServiceName.GOOGLE_GEOCODING)
        result = GeocodingClient(
            api_key=api_key,
            use_mock=False,
            db=self.db,
        ).reverse_geocode(
            latitude=latitude,
            longitude=longitude,
            language=language,
        )
        cache.save(
            latitude=latitude,
            longitude=longitude,
            country=result.get("country"),
            province=result.get("province"),
            city=result.get("city"),
            district=result.get("district"),
            place_name=result.get("place_name"),
            provider="GOOGLE",
        )
        return {
            "display_name": result.get("place_name"),
            "latitude": result.get("latitude", latitude),
            "longitude": result.get("longitude", longitude),
            "country": result.get("country"),
            "province": result.get("province"),
            "city": result.get("city"),
            "district": result.get("district"),
            "place_name": result.get("place_name"),
            "provider": "google_geocoding",
            "source": "provider",
        }

    def forward_geocode(
        self,
        *,
        query: str,
        language: str,
    ) -> list[dict[str, object]]:
        api_key = self._require_key(ExternalServiceName.GOOGLE_GEOCODING)
        return GeocodingClient(
            api_key=api_key,
            use_mock=False,
            db=self.db,
        ).forward_geocode(query=query, language=language)

    def places_autocomplete(
        self,
        *,
        query: str,
        language: str,
        session_token: str | None,
    ) -> list[dict[str, object]]:
        return self._places_client().autocomplete(
            query=query,
            language=language,
            session_token=session_token,
        )

    def place_details(
        self,
        *,
        place_id: str,
        language: str,
        session_token: str | None,
    ) -> dict[str, object]:
        return self._places_client().details(
            place_id=place_id,
            language=language,
            session_token=session_token,
        )

    def places_search(
        self,
        *,
        query: str,
        language: str,
    ) -> list[dict[str, object]]:
        return self._places_client().search(query=query, language=language)

    def current_weather(
        self,
        *,
        latitude: float,
        longitude: float,
        language: str,
    ) -> dict[str, object]:
        return self._weather_client().get_weather(
            latitude=latitude,
            longitude=longitude,
            language=language,
        )

    def forecast(
        self,
        *,
        latitude: float,
        longitude: float,
        language: str,
    ) -> list[dict[str, object]]:
        return self._weather_client().get_forecast(
            latitude=latitude,
            longitude=longitude,
            language=language,
        )

    def _places_client(self) -> PlacesClient:
        return PlacesClient(
            api_key=self._require_key(ExternalServiceName.GOOGLE_PLACES),
            db=self.db,
        )

    def _weather_client(self) -> WeatherClient:
        return WeatherClient(
            api_key=self._require_key(ExternalServiceName.WEATHER),
            db=self.db,
        )

    def _require_key(self, service: ExternalServiceName) -> str:
        key = self.keys.resolve(service)
        if not key:
            raise ApiClientError(
                f"{service.value} is not configured",
                code=ExternalApiErrorCode.API_KEY_NOT_CONFIGURED,
            )
        return key

