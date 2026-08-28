"""SpaceCatalog Astronomy Events provider adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from requests import Session as HttpSession

from app.common.services.api_clients.base_client import (
    ApiClientError,
    BaseClient,
    ExternalApiErrorCode,
)


class SpaceCatalogEventsClient(BaseClient):
    """Read dated astronomy events from the public SpaceCatalog API."""

    EVENT_KINDS = (
        "opposition",
        "elongation",
        "conjunction",
        "eclipse",
        "shower",
    )

    def __init__(self, *, session: HttpSession | None = None) -> None:
        super().__init__(
            base_url="https://spacecatalog.org/api/v1",
            session=session,
        )

    def list_events(
        self,
        *,
        from_at: datetime,
        to_at: datetime,
    ) -> list[dict[str, Any]]:
        try:
            payload = self.get(
                "/events",
                params={
                    "from": self._format_utc(from_at),
                    "to": self._format_utc(to_at),
                    "kind": list(self.EVENT_KINDS),
                    "limit": 500,
                    "format": "json",
                },
            )
        except ApiClientError as exc:
            if exc.status_code == 429:
                raise ApiClientError(
                    "SpaceCatalog rate limit exceeded",
                    code=ExternalApiErrorCode.API_LIMIT_EXCEEDED,
                    status_code=exc.status_code,
                ) from exc
            if exc.status_code == 400:
                raise ApiClientError(
                    "SpaceCatalog rejected the event query",
                    code=ExternalApiErrorCode.INVALID_REQUEST,
                    status_code=exc.status_code,
                ) from exc
            raise

        events = payload.get("events")
        if not isinstance(events, list):
            raise ApiClientError("SpaceCatalog returned an invalid events payload")
        if not all(isinstance(item, dict) for item in events):
            raise ApiClientError("SpaceCatalog returned an invalid event record")
        return events

    @staticmethod
    def _format_utc(value: datetime) -> str:
        return (
            value.astimezone(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
