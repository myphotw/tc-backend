"""Astrometry.net API Client."""

from __future__ import annotations

from typing import Any

from app.common.config import settings
from app.common.services.api_clients.base_client import BaseClient


class AstrometryClient(BaseClient):
    """
    Astrometry.net Plate Solve API Client.

    Plugin은 이 Client만 호출한다.
    현재 단계에서는 실제 API를 호출하지 않는다.
    """

    def __init__(self, *, api_key: str | None = None) -> None:
        super().__init__(base_url="https://nova.astrometry.net")
        self.api_key = api_key if api_key is not None else settings.ASTROMETRY_API_KEY

    def solve(self, *, image_path: str) -> dict[str, Any]:
        """
        천체 이미지 Plate Solve를 수행한다.

        Args:
            image_path: Plate Solve 대상 이미지 경로

        Returns:
            dict[str, Any]: Mock plate solve 결과
        """
        self.logger.info(
            "AstrometryClient.solve mock call image_path=%s api_key_configured=%s",
            image_path,
            bool(self.api_key),
        )
        return {
            "provider": "astrometry",
            "status": "mock",
            "image_path": image_path,
            "ra": None,
            "dec": None,
            "rotation": None,
            "fov": None,
            "message": "Astrometry API is not implemented yet",
        }
