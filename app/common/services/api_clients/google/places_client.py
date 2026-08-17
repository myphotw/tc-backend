"""Google Places legacy web-service adapter used by AstroJournal Phase 1."""

from __future__ import annotations

from typing import Any

from requests import Session as HttpSession
from sqlalchemy.orm import Session

from app.common.repositories.api_usage_repository import ApiName, ApiProvider
from app.common.services.api_clients.base_client import ApiClientError, BaseClient
from app.common.services.api_clients.google.geocoding_client import GeocodingClient


class PlacesClient(BaseClient):
    def __init__(
        self,
        *,
        api_key: str,
        db: Session | None = None,
        session: HttpSession | None = None,
    ) -> None:
        super().__init__(
            base_url="https://maps.googleapis.com",
            db=db,
            provider=ApiProvider.GOOGLE,
            api_name=ApiName.PLACES,
            session=session,
        )
        self.api_key = api_key

    def autocomplete(
        self,
        *,
        query: str,
        language: str = "ko",
        session_token: str | None = None,
    ) -> list[dict[str, Any]]:
        params = {
            "input": query,
            "language": language,
            "components": "country:kr",
            "location": "37.5,127.0",
            "radius": "500000",
            "key": self.api_key,
        }
        if session_token:
            params["sessiontoken"] = session_token
        payload = self.get("/maps/api/place/autocomplete/json", params=params)
        self._validate_status(payload)
        items = []
        for prediction in payload.get("predictions") or []:
            place_id = prediction.get("place_id")
            structured = prediction.get("structured_formatting") or {}
            main_text = structured.get("main_text") or prediction.get("description")
            if not place_id or not main_text:
                continue
            secondary_text = structured.get("secondary_text")
            items.append(
                {
                    "place_id": place_id,
                    "main_text": main_text,
                    "secondary_text": secondary_text,
                    "display_name": (
                        f"{main_text} · {secondary_text}"
                        if secondary_text
                        else main_text
                    ),
                }
            )
        self.track_usage(units=1)
        return items

    def details(
        self,
        *,
        place_id: str,
        language: str = "ko",
        session_token: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "place_id": place_id,
            "fields": "place_id,geometry,formatted_address,name,address_components",
            "language": language,
            "key": self.api_key,
        }
        if session_token:
            params["sessiontoken"] = session_token
        payload = self.get("/maps/api/place/details/json", params=params)
        self._validate_status(payload)
        result = self._normalize_place(payload.get("result") or {})
        if result is None:
            raise ApiClientError("Places details response was incomplete")
        self.track_usage(units=1)
        return result

    def search(
        self,
        *,
        query: str,
        language: str = "ko",
    ) -> list[dict[str, Any]]:
        payload = self.get(
            "/maps/api/place/textsearch/json",
            params={
                "query": query,
                "language": language,
                "region": "kr",
                "key": self.api_key,
            },
        )
        self._validate_status(payload)
        items = [
            item
            for raw in payload.get("results") or []
            if (item := self._normalize_place(raw)) is not None
        ]
        self.track_usage(units=1)
        return items

    @staticmethod
    def _validate_status(payload: dict[str, Any]) -> None:
        if payload.get("status") not in {"OK", "ZERO_RESULTS"}:
            raise ApiClientError("Google Places provider returned an error")

    @staticmethod
    def _normalize_place(raw: dict[str, Any]) -> dict[str, Any] | None:
        location = (raw.get("geometry") or {}).get("location") or {}
        latitude = location.get("lat")
        longitude = location.get("lng")
        name = raw.get("name")
        formatted = raw.get("formatted_address") or name
        if latitude is None or longitude is None or not formatted:
            return None
        mapped = GeocodingClient._map_address_components(
            raw.get("address_components") or []
        )
        return {
            "place_id": raw.get("place_id"),
            "display_name": formatted,
            "latitude": float(latitude),
            "longitude": float(longitude),
            **mapped,
            "place_name": name,
            "provider": "google_places",
        }
