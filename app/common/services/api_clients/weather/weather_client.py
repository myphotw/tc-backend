"""Weather API Client."""

from __future__ import annotations

from typing import Any

from app.common.config import settings
from app.common.services.api_clients.base_client import BaseClient


class WeatherClient(BaseClient):
    """
    Weather API Client.

    Plugin은 이 Client만 호출한다.
    현재 단계에서는 실제 API를 호출하지 않는다.
    """

    def __init__(self, *, api_key: str | None = None) -> None:
        super().__init__(base_url="https://api.openweathermap.org")
        self.api_key = api_key if api_key is not None else settings.WEATHER_API_KEY

    def get_weather(
        self,
        *,
        latitude: float,
        longitude: float,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        """
        좌표 기준 날씨 정보를 조회한다.

        Args:
            latitude: 위도
            longitude: 경도
            observed_at: 관측 시각 (ISO 문자열)

        Returns:
            dict[str, Any]: Mock weather 결과
        """
        self.logger.info(
            "WeatherClient.get_weather mock call lat=%s lon=%s observed_at=%s api_key_configured=%s",
            latitude,
            longitude,
            observed_at,
            bool(self.api_key),
        )
        return {
            "provider": "weather",
            "status": "mock",
            "latitude": latitude,
            "longitude": longitude,
            "observed_at": observed_at,
            "weather": None,
            "temperature": None,
            "humidity": None,
            "message": "Weather API is not implemented yet",
        }
