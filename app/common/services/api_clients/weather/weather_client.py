"""Weather API Client."""

from __future__ import annotations

from datetime import datetime, timezone
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


class WeatherClient(BaseClient):
    """
    Weather API Client.

    OpenWeatherMap current weather and 5-day/3-hour forecast adapter.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        db: Session | None = None,
        session: HttpSession | None = None,
    ) -> None:
        super().__init__(
            base_url="https://api.openweathermap.org/data/2.5",
            db=db,
            provider=ApiProvider.WEATHER,
            api_name=ApiName.WEATHER,
            session=session,
        )
        self.api_key = api_key if api_key is not None else settings.WEATHER_API_KEY

    def get_weather(
        self,
        *,
        latitude: float,
        longitude: float,
        observed_at: str | None = None,
        language: str = "ko",
    ) -> dict[str, Any]:
        """
        좌표 기준 날씨 정보를 조회한다.

        Args:
            latitude: 위도
            longitude: 경도
            observed_at: 관측 시각 (ISO 문자열)

        Returns:
            dict[str, Any]: normalized current weather
        """
        if not self.api_key:
            raise ApiClientError(
                "Weather API key is not configured",
                code=ExternalApiErrorCode.API_KEY_NOT_CONFIGURED,
            )
        payload = self.get(
            "/weather",
            params=self._params(latitude, longitude, language),
        )
        result = self._normalize_current(payload)
        if observed_at:
            result["observed_at"] = observed_at
        self.track_usage(units=1)
        return result

    def get_forecast(
        self,
        *,
        latitude: float,
        longitude: float,
        language: str = "ko",
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise ApiClientError(
                "Weather API key is not configured",
                code=ExternalApiErrorCode.API_KEY_NOT_CONFIGURED,
            )
        payload = self.get(
            "/forecast",
            params=self._params(latitude, longitude, language),
        )
        items = [self._normalize_forecast(item) for item in payload.get("list") or []]
        self.track_usage(units=1)
        return items

    def _params(
        self,
        latitude: float,
        longitude: float,
        language: str,
    ) -> dict[str, Any]:
        return {
            "lat": latitude,
            "lon": longitude,
            "appid": self.api_key,
            "units": "metric",
            "lang": "kr" if language == "ko" else language,
        }

    @classmethod
    def _normalize_current(cls, payload: dict[str, Any]) -> dict[str, Any]:
        main = payload.get("main") or {}
        wind = payload.get("wind") or {}
        clouds = payload.get("clouds") or {}
        weather = (payload.get("weather") or [{}])[0]
        sys = payload.get("sys") or {}
        return {
            "provider": "openweathermap",
            "temperature": cls._number(main.get("temp")),
            "feels_like": cls._number(main.get("feels_like")),
            "humidity": main.get("humidity"),
            "pressure": main.get("pressure"),
            "clouds": clouds.get("all"),
            "wind_speed": cls._number(wind.get("speed")),
            "wind_direction": wind.get("deg"),
            "weather_code": weather.get("id"),
            "description": weather.get("description"),
            "icon": weather.get("icon"),
            "visibility": payload.get("visibility"),
            "city_name": payload.get("name"),
            "observed_at": cls._timestamp(payload.get("dt")),
            "sunrise": cls._timestamp(sys.get("sunrise")),
            "sunset": cls._timestamp(sys.get("sunset")),
        }

    @classmethod
    def _normalize_forecast(cls, payload: dict[str, Any]) -> dict[str, Any]:
        main = payload.get("main") or {}
        wind = payload.get("wind") or {}
        clouds = payload.get("clouds") or {}
        weather = (payload.get("weather") or [{}])[0]
        rain = payload.get("rain") or {}
        return {
            "timestamp": cls._timestamp(payload.get("dt")),
            "temperature": cls._number(main.get("temp")),
            "feels_like": cls._number(main.get("feels_like")),
            "humidity": main.get("humidity"),
            "pressure": main.get("pressure"),
            "clouds": clouds.get("all"),
            "wind_speed": cls._number(wind.get("speed")),
            "wind_direction": wind.get("deg"),
            "weather_code": weather.get("id"),
            "description": weather.get("description"),
            "icon": weather.get("icon"),
            "visibility": payload.get("visibility"),
            "precipitation_probability": cls._number(payload.get("pop")),
            "rain_volume_mm": cls._number(rain.get("3h") or rain.get("1h")),
        }

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        if value is None:
            return None
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()

    @staticmethod
    def _number(value: Any) -> float | None:
        return float(value) if value is not None else None
