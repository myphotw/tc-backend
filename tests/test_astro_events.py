from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.astrojournal.routers.events import list_astronomy_events
from app.astrojournal.services.astronomy_event_service import (
    AstronomyEventMemoryCache,
    AstronomyEventService,
)
from app.common.services.api_clients.base_client import (
    ApiClientError,
    ExternalApiErrorCode,
)
from app.common.services.api_clients.spacecatalog import SpaceCatalogEventsClient
from app.main import app


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.content = b"{}"

    def json(self):
        return self.payload


class FakeHttpSession:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        pass


class FakeEventsClient:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[datetime, datetime]] = []

    def list_events(self, *, from_at: datetime, to_at: datetime):
        self.calls.append((from_at, to_at))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def event(
    event_id: str,
    kind: str,
    at: str,
    *,
    title: str = "",
    objects: list[str] | None = None,
    circumstances: dict | None = None,
    window: dict | None = None,
) -> dict:
    item = {
        "id": event_id,
        "kind": kind,
        "time": at,
        "title": title,
        "objects": objects or [],
        "circumstances": circumstances or {},
    }
    if window is not None:
        item["window"] = window
    return item


class AstroEventsTests(unittest.TestCase):
    NOW = datetime(2026, 8, 28, 6, tzinfo=timezone.utc)
    FROM = datetime(2026, 8, 28, tzinfo=timezone.utc)
    TO = datetime(2027, 2, 28, tzinfo=timezone.utc)

    def service(self, client: FakeEventsClient, *, now: datetime | None = None):
        return AstronomyEventService(
            client=client,
            cache=AstronomyEventMemoryCache(),
            clock=lambda: now or self.NOW,
        )

    def test_spacecatalog_response_parsing_and_query_contract(self) -> None:
        raw = event(
            "opposition-saturn-2026-10-04",
            "opposition",
            "2026-10-04T12:12:54.000Z",
            title="Saturn at opposition",
            objects=["saturn"],
        )
        session = FakeHttpSession(FakeResponse({"events": [raw]}))
        client = SpaceCatalogEventsClient(session=session)

        result = client.list_events(from_at=self.FROM, to_at=self.TO)

        self.assertEqual(result, [raw])
        request = session.requests[0]
        self.assertTrue(request["url"].endswith("/api/v1/events"))
        self.assertEqual(request["params"]["from"], "2026-08-28T00:00:00Z")
        self.assertEqual(request["params"]["to"], "2027-02-28T00:00:00Z")
        self.assertEqual(
            request["params"]["kind"],
            ["opposition", "elongation", "conjunction", "eclipse", "shower"],
        )
        self.assertEqual(request["params"]["limit"], 500)

    def test_normalization_titles_tags_time_sort_filter_and_deduplicate(self) -> None:
        completed = event(
            "opposition-mars-2026-08-28",
            "opposition",
            "2026-08-28T05:00:00Z",
            objects=["mars"],
        )
        conjunction = event(
            "conjunction-moon-jupiter-2026-09-08",
            "conjunction",
            "2026-09-08T18:43:54.32Z",
            objects=["moon", "jupiter"],
            circumstances={"separation_deg": 0.77},
        )
        opposition = event(
            "opposition-saturn-2026-10-04",
            "opposition",
            "2026-10-04T12:12:54.000Z",
            objects=["saturn"],
        )
        eclipse = event(
            "eclipse-solar-2027-02-06",
            "eclipse",
            "2027-02-06T15:59:32.000Z",
            title="Annular solar eclipse",
            objects=["sun", "moon"],
        )
        major_shower = event(
            "shower-geminids-2026-12-14",
            "shower",
            "2026-12-14T04:52:37.000Z",
            title="Geminids at maximum",
            objects=["3200-phaethon"],
            circumstances={"zhr": 150},
            window={
                "start": "2026-12-07T02:52:47.000Z",
                "end": "2026-12-19T21:41:02.000Z",
            },
        )
        minor_shower = event(
            "shower-draconids-2026-10-08",
            "shower",
            "2026-10-08T16:13:20.000Z",
            circumstances={"zhr": 5},
            window={
                "start": "2026-10-07T18:20:32.000Z",
                "end": "2026-10-09T18:56:52.000Z",
            },
        )
        excluded_moon_quarter = event(
            "moon-q2-2026-09-26",
            "moon",
            "2026-09-26T00:00:00Z",
            title="Full moon",
            objects=["moon"],
        )
        client = FakeEventsClient(
            [
                eclipse,
                major_shower,
                opposition,
                conjunction,
                conjunction,
                minor_shower,
                excluded_moon_quarter,
                completed,
            ]
        )

        response = self.service(client).list_events(
            from_at=self.FROM,
            to_at=self.TO,
        )

        self.assertEqual(
            [item.id for item in response.events],
            [conjunction["id"], opposition["id"], major_shower["id"], eclipse["id"]],
        )
        by_id = {item.id: item for item in response.events}
        self.assertEqual(by_id[conjunction["id"]].title, "달·목성 근접")
        self.assertEqual(by_id[conjunction["id"]].type, "conjunction")
        self.assertEqual(by_id[opposition["id"]].title, "토성 관측 최적기")
        self.assertEqual(by_id[opposition["id"]].tags, ["망원경 관측", "행성 촬영"])
        self.assertEqual(by_id[eclipse["id"]].title, "금환일식")
        self.assertEqual(by_id[eclipse["id"]].tags[0], "보호장비 필수")
        shower = by_id[major_shower["id"]]
        self.assertEqual(shower.title, "쌍둥이자리 유성우")
        self.assertEqual(shower.tags, ["맨눈 관측", "광시야 촬영"])
        self.assertEqual(shower.start_at.isoformat(), "2026-12-07T02:52:47+00:00")
        self.assertEqual(shower.end_at.isoformat(), "2026-12-19T21:41:02+00:00")
        self.assertIsNotNone(shower.peak_at.utcoffset())

    def test_elongation_and_lunar_eclipse_title_mapping(self) -> None:
        client = FakeEventsClient(
            [
                event(
                    "elongation-venus-2027-01-03",
                    "elongation",
                    "2027-01-03T17:56:03Z",
                    objects=["venus"],
                ),
                event(
                    "eclipse-lunar-2027-02-20",
                    "eclipse",
                    "2027-02-20T23:12:44Z",
                    title="Penumbral lunar eclipse",
                    objects=["moon", "earth"],
                ),
            ]
        )
        items = self.service(client).list_events(
            from_at=self.FROM,
            to_at=self.TO,
        ).events
        self.assertEqual(items[0].title, "금성 관측 최적기")
        self.assertEqual(items[0].type, "planet_viewing")
        self.assertEqual(items[1].title, "반영월식")
        self.assertEqual(items[1].type, "lunar_eclipse")

    def test_cache_hit_does_not_call_provider_again(self) -> None:
        raw = event(
            "opposition-saturn-2026-10-04",
            "opposition",
            "2026-10-04T12:12:54Z",
            objects=["saturn"],
        )
        client = FakeEventsClient([raw])
        service = self.service(client)

        first = service.list_events(from_at=self.FROM, to_at=self.TO)
        second = service.list_events(from_at=self.FROM, to_at=self.TO)

        self.assertEqual(first, second)
        self.assertEqual(len(client.calls), 1)

    def test_provider_failure_returns_expired_stale_cache(self) -> None:
        raw = event(
            "opposition-saturn-2026-10-04",
            "opposition",
            "2026-10-04T12:12:54Z",
            objects=["saturn"],
        )
        provider_error = ApiClientError("provider unavailable")
        client = FakeEventsClient([raw], provider_error)
        current = [self.NOW]
        service = AstronomyEventService(
            client=client,
            cache=AstronomyEventMemoryCache(),
            clock=lambda: current[0],
        )
        first = service.list_events(from_at=self.FROM, to_at=self.TO)
        current[0] += timedelta(hours=25)

        stale = service.list_events(from_at=self.FROM, to_at=self.TO)

        self.assertEqual(stale, first)
        self.assertEqual(len(client.calls), 2)

    def test_default_range_uses_latest_stale_cache_after_date_changes(self) -> None:
        raw = event(
            "opposition-saturn-2026-10-04",
            "opposition",
            "2026-10-04T12:12:54Z",
            objects=["saturn"],
        )
        client = FakeEventsClient([raw], ApiClientError("provider unavailable"))
        current = [self.NOW]
        service = AstronomyEventService(
            client=client,
            cache=AstronomyEventMemoryCache(),
            clock=lambda: current[0],
        )
        service.list_events()
        current[0] += timedelta(hours=25)

        stale = service.list_events()

        self.assertEqual(stale.events[0].id, raw["id"])
        self.assertEqual(len(client.calls), 2)

    def test_provider_failure_without_cache_is_propagated(self) -> None:
        expected = ApiClientError(
            "provider unavailable",
            code=ExternalApiErrorCode.PROVIDER_ERROR,
        )
        service = self.service(FakeEventsClient(expected))
        with self.assertRaises(ApiClientError) as raised:
            service.list_events(from_at=self.FROM, to_at=self.TO)
        self.assertIs(raised.exception, expected)

    def test_from_to_validation(self) -> None:
        service = self.service(FakeEventsClient([]))
        invalid_ranges = (
            (self.TO, self.FROM),
            (self.FROM, self.FROM),
            (self.FROM, datetime(2028, 8, 29, tzinfo=timezone.utc)),
            (datetime(2026, 8, 28), self.TO),
        )
        for from_at, to_at in invalid_ranges:
            with self.subTest(from_at=from_at, to_at=to_at):
                with self.assertRaises(ApiClientError) as raised:
                    service.list_events(from_at=from_at, to_at=to_at)
                self.assertEqual(
                    raised.exception.code,
                    ExternalApiErrorCode.INVALID_REQUEST,
                )

    def test_openapi_and_provider_error_http_contract(self) -> None:
        operation = app.openapi()["paths"]["/api/astro/events"]["get"]
        self.assertEqual(
            {parameter["name"] for parameter in operation["parameters"]},
            {"from", "to"},
        )
        response_ref = operation["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        self.assertTrue(response_ref.endswith("/AstronomyEventListResponse"))
        self.assertEqual(operation["security"], [{"TCBackendBearer": []}])

        with patch.object(
            AstronomyEventService,
            "list_events",
            side_effect=ApiClientError("provider unavailable"),
        ):
            with self.assertRaises(HTTPException) as raised:
                list_astronomy_events(from_at=self.FROM, to_at=self.TO)
            self.assertEqual(raised.exception.status_code, 502)


if __name__ == "__main__":
    unittest.main()
