"""Google Geocoding API Client."""

from __future__ import annotations

from typing import Any

from requests import Session as HttpSession
from sqlalchemy.orm import Session

from app.common.config import settings
from app.common.repositories.api_usage_repository import ApiName, ApiProvider
from app.common.services.api_clients.base_client import (
    ApiClientError,
    BaseClient,
    ExternalApiErrorCode,
)


class GeocodingClient(BaseClient):
    """
    Google Geocoding API Client.

    Plugin은 이 Client만 호출한다.
    GOOGLE_API_KEY가 없으면 Mock, 있으면 실제 API 호출 구조로 전환한다.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        use_mock: bool | None = None,
        db: Session | None = None,
        session: HttpSession | None = None,
    ) -> None:
        super().__init__(
            base_url="https://maps.googleapis.com",
            db=db,
            provider=ApiProvider.GOOGLE,
            api_name=ApiName.GEOCODING,
            session=session,
        )
        self.api_key = api_key if api_key is not None else settings.GOOGLE_API_KEY
        if use_mock is None:
            self.use_mock = not bool(self.api_key)
        else:
            self.use_mock = use_mock

    def reverse_geocode(
        self,
        *,
        latitude: float,
        longitude: float,
        language: str = "ko",
    ) -> dict[str, Any]:
        """
        GPS 좌표를 주소로 변환한다.

        Args:
            latitude: 위도
            longitude: 경도

        Returns:
            dict[str, Any]: reverse geocode 결과
        """
        self.logger.info(
            "GeocodingClient.reverse_geocode lat=%s lon=%s use_mock=%s api_key_configured=%s",
            latitude,
            longitude,
            self.use_mock,
            bool(self.api_key),
        )
        if self.use_mock:
            return self._mock_response(latitude=latitude, longitude=longitude)
        return self._request_reverse_geocode(
            latitude=latitude,
            longitude=longitude,
            language=language,
        )

    def _request_reverse_geocode(
        self,
        *,
        latitude: float,
        longitude: float,
        language: str,
    ) -> dict[str, Any]:
        """실제 Google Geocoding API를 호출한다."""
        if not self.api_key:
            raise ApiClientError(
                "Google Geocoding key is not configured",
                code=ExternalApiErrorCode.API_KEY_NOT_CONFIGURED,
            )

        payload = self.get(
            "/maps/api/geocode/json",
            params={
                "latlng": f"{latitude},{longitude}",
                "key": self.api_key,
                "language": language,
            },
        )
        result = self._parse_geocode_payload(
            payload,
            latitude=latitude,
            longitude=longitude,
        )
        self.track_usage(units=1)
        return result

    def forward_geocode(
        self,
        *,
        query: str,
        language: str = "ko",
    ) -> list[dict[str, Any]]:
        """Resolve an address or place query into normalized candidates."""
        if not self.api_key:
            raise ApiClientError(
                "Google Geocoding key is not configured",
                code=ExternalApiErrorCode.API_KEY_NOT_CONFIGURED,
            )
        payload = self.get(
            "/maps/api/geocode/json",
            params={
                "address": query,
                "key": self.api_key,
                "language": language,
                "region": "kr",
            },
        )
        status = payload.get("status")
        if status not in {"OK", "ZERO_RESULTS"}:
            raise ApiClientError("Geocoding provider returned an error")
        items = [
            item
            for result in payload.get("results") or []
            if (item := self._normalize_candidate(result)) is not None
        ]
        self.track_usage(units=1)
        return items

    def _parse_geocode_payload(
        self,
        payload: dict[str, Any],
        *,
        latitude: float,
        longitude: float,
    ) -> dict[str, Any]:
        """Google Geocoding 응답을 Metadata 필드로 정규화한다."""
        status = payload.get("status")
        if status not in {"OK", "ZERO_RESULTS"}:
            raise ApiClientError("Geocoding provider returned an error")

        results = payload.get("results") or []
        if not results:
            return {
                "provider": "google_geocoding",
                "status": "empty",
                "latitude": latitude,
                "longitude": longitude,
                "country": None,
                "province": None,
                "city": None,
                "district": None,
                "place_name": None,
            }

        first = results[0]
        components = first.get("address_components") or []
        mapped = self._map_address_components(components)
        return {
            "provider": "google_geocoding",
            "status": "ok",
            "latitude": latitude,
            "longitude": longitude,
            "country": mapped.get("country"),
            "province": mapped.get("province"),
            "city": mapped.get("city"),
            "district": mapped.get("district"),
            "place_name": first.get("formatted_address"),
        }

    @staticmethod
    def _map_address_components(
        components: list[dict[str, Any]],
    ) -> dict[str, str | None]:
        """Google address_components를 country/province/city/district로 매핑한다."""
        mapped = {
            "country": None,
            "province": None,
            "city": None,
            "district": None,
        }
        for component in components:
            types = set(component.get("types") or [])
            name = component.get("long_name")
            if not name:
                continue
            if "country" in types:
                mapped["country"] = name
            elif "administrative_area_level_1" in types:
                mapped["province"] = name
            elif "locality" in types or "administrative_area_level_2" in types:
                if mapped["city"] is None:
                    mapped["city"] = name
            elif (
                "sublocality_level_1" in types
                or "sublocality" in types
                or "administrative_area_level_3" in types
            ):
                if mapped["district"] is None:
                    mapped["district"] = name
        return mapped

    def _normalize_candidate(
        self,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        location = (result.get("geometry") or {}).get("location") or {}
        latitude = location.get("lat")
        longitude = location.get("lng")
        display_name = result.get("formatted_address")
        if latitude is None or longitude is None or not display_name:
            return None
        components = result.get("address_components") or []
        mapped = self._map_address_components(components)
        return {
            "display_name": display_name,
            "latitude": float(latitude),
            "longitude": float(longitude),
            **mapped,
            "place_name": self._primary_place_name(components),
            "provider": "google_geocoding",
        }

    @staticmethod
    def _primary_place_name(components: list[dict[str, Any]]) -> str | None:
        for target in (
            "administrative_area_level_3",
            "sublocality_level_1",
            "locality",
            "administrative_area_level_2",
            "administrative_area_level_1",
        ):
            for component in components:
                if target in set(component.get("types") or []):
                    value = component.get("long_name")
                    if value:
                        return str(value)
        return None

    def _mock_response(
        self,
        *,
        latitude: float,
        longitude: float,
    ) -> dict[str, Any]:
        """Mock reverse geocode 결과를 반환한다."""
        return {
            "provider": "google_geocoding",
            "status": "mock",
            "latitude": latitude,
            "longitude": longitude,
            "country": "Republic of Korea",
            "province": "Seoul",
            "city": "Gangnam-gu",
            "district": "Yeoksam-dong",
            "place_name": "Mock Place",
            "message": "Geocoding mock response",
        }
