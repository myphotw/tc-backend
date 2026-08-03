"""Google Geocoding API Client."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.common.config import settings
from app.common.repositories.api_usage_repository import ApiName, ApiProvider
from app.common.services.api_clients.base_client import ApiClientError, BaseClient


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
    ) -> None:
        super().__init__(
            base_url="https://maps.googleapis.com",
            db=db,
            provider=ApiProvider.GOOGLE,
            api_name=ApiName.GEOCODING,
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
            if not self.can_use(units=1):
                raise ApiClientError(
                    "API usage limit exceeded: provider=GOOGLE api_name=GEOCODING"
                )
            result = self._mock_response(latitude=latitude, longitude=longitude)
            self.track_usage(units=1)
            return result
        return self._request_reverse_geocode(latitude=latitude, longitude=longitude)

    def _request_reverse_geocode(
        self,
        *,
        latitude: float,
        longitude: float,
    ) -> dict[str, Any]:
        """실제 Google Geocoding API를 호출한다."""
        if not self.api_key:
            raise ApiClientError("GOOGLE_API_KEY is not configured")

        payload = self.get(
            "/maps/api/geocode/json",
            params={
                "latlng": f"{latitude},{longitude}",
                "key": self.api_key,
                "language": "ko",
            },
        )
        return self._parse_geocode_payload(
            payload,
            latitude=latitude,
            longitude=longitude,
        )

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
            raise ApiClientError(f"Geocoding API status={status}")

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

    def _map_address_components(
        self,
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
