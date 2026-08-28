from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.astrojournal.services.plate_solve_service import PlateSolveService
from app.common.database import Base
from app.common.models.api_key import ApiKey
from app.common.models.api_usage import CommonApiUsage
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.repositories.api_usage_repository import ApiName, ApiProvider
from app.common.repositories.geocode_cache_repository import GeocodeCacheRepository
from app.common.routers.api_keys import get_api_keys
from app.common.routers.capabilities import capabilities
from app.common.routers.external_apis import current_weather
from app.common.security.crypto import encrypt_value
from app.common.services.api_clients.astrometry import (
    AstrometryClient,
    AstrometryProviderWorkNotFound,
)
from app.common.services.api_clients.base_client import (
    ApiClientError,
    ExternalApiErrorCode,
)
from app.common.services.api_clients.google import GeocodingClient, PlacesClient
from app.common.services.api_clients.weather import WeatherClient
from app.common.services.key_resolver import (
    ExternalServiceName,
    KeyResolver,
    KeySource,
)
from app.common.services.external_api_service import ExternalApiService
from app.common.services.monitoring_service import check_external_readiness
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


def geocode_payload() -> dict:
    return {
        "status": "OK",
        "results": [
            {
                "formatted_address": "Seoul, Republic of Korea",
                "geometry": {"location": {"lat": 37.5, "lng": 127.0}},
                "address_components": [
                    {"long_name": "Republic of Korea", "types": ["country"]},
                    {
                        "long_name": "Seoul",
                        "types": ["administrative_area_level_1"],
                    },
                ],
            }
        ],
    }


class ExternalApiPhase1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def settings_stub(**overrides):
        values = {
            "GOOGLE_API_KEY": None,
            "WEATHER_API_KEY": None,
            "ASTROMETRY_API_KEY": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_key_resolver_db_priority_env_fallback_disabled_and_decrypt_failure(self):
        self.db.add(
            ApiKey(
                service_name=ExternalServiceName.WEATHER.value,
                api_key=encrypt_value("db-weather-value"),
                enabled=True,
            )
        )
        self.db.commit()
        resolver = KeyResolver(
            self.db,
            settings_value=self.settings_stub(WEATHER_API_KEY="env-weather-value"),
        )
        resolved = resolver.resolve_with_source(ExternalServiceName.WEATHER)
        self.assertEqual(resolved.source, KeySource.DATABASE)
        self.assertEqual(resolved.value, "db-weather-value")

        item = self.db.query(ApiKey).one()
        item.enabled = False
        self.db.commit()
        resolved = resolver.resolve_with_source(ExternalServiceName.WEATHER)
        self.assertEqual(resolved.source, KeySource.ENVIRONMENT)

        item.enabled = True
        item.api_key = "not-a-valid-token"
        self.db.commit()
        resolved = resolver.resolve_with_source(ExternalServiceName.WEATHER)
        self.assertEqual(resolved.source, KeySource.ENVIRONMENT)

        resolver = KeyResolver(self.db, settings_value=self.settings_stub())
        self.assertIsNone(resolver.resolve(ExternalServiceName.WEATHER))

    def test_api_key_list_masks_encrypted_material(self):
        encrypted = encrypt_value("stored-value")
        self.db.add(
            ApiKey(
                service_name=ExternalServiceName.ASTROMETRY.value,
                api_key=encrypted,
                enabled=True,
                description="plate solve",
            )
        )
        self.db.commit()
        result = get_api_keys(self.db)
        payload = result[0].model_dump()
        self.assertNotIn("api_key", payload)
        self.assertNotIn(encrypted, str(payload))
        self.assertEqual(payload["masked"], "****")

    def test_geocoding_reverse_forward_usage_and_mock_does_not_count(self):
        session = FakeHttpSession(FakeResponse(geocode_payload()), FakeResponse(geocode_payload()))
        client = GeocodingClient(
            api_key="test-google-value",
            use_mock=False,
            db=self.db,
            session=session,
        )
        reverse = client.reverse_geocode(latitude=37.5, longitude=127.0)
        forward = client.forward_geocode(query="Seoul")
        self.assertEqual(reverse["province"], "Seoul")
        self.assertEqual(forward[0]["latitude"], 37.5)
        usage = self.db.query(CommonApiUsage).filter_by(
            provider=ApiProvider.GOOGLE,
            api_name=ApiName.GEOCODING,
        ).one()
        self.assertEqual(usage.used_unit, 2)

        other_db = self.Session()
        try:
            GeocodingClient(use_mock=True, db=other_db).reverse_geocode(
                latitude=0,
                longitude=0,
            )
            self.assertEqual(other_db.query(CommonApiUsage).count(), 1)
            # The existing usage row belongs to the shared in-memory database;
            # a mock call itself must not change its count.
            self.assertEqual(usage.used_unit, 2)
        finally:
            other_db.close()

        GeocodeCacheRepository(self.db).save(
            latitude=35,
            longitude=128,
            city="Cached City",
            place_name="Cached Place",
        )
        with patch(
            "app.common.services.external_api_service.GeocodingClient.reverse_geocode"
        ) as provider:
            cached = ExternalApiService(self.db).reverse_geocode(
                latitude=35,
                longitude=128,
                language="ko",
            )
        provider.assert_not_called()
        self.assertEqual(cached["source"], "cache")
        self.assertEqual(usage.used_unit, 2)

        denied = GeocodingClient(
            api_key="test-google-value",
            use_mock=False,
            session=FakeHttpSession(FakeResponse({"status": "REQUEST_DENIED"})),
        )
        with self.assertRaises(ApiClientError):
            denied.forward_geocode(query="Seoul")

    def test_places_autocomplete_details_search_and_usage(self):
        session = FakeHttpSession(
            FakeResponse(
                {
                    "status": "OK",
                    "predictions": [
                        {
                            "place_id": "place-1",
                            "description": "Seoul",
                            "structured_formatting": {
                                "main_text": "Seoul",
                                "secondary_text": "Republic of Korea",
                            },
                        }
                    ],
                }
            ),
            FakeResponse(
                {
                    "status": "OK",
                    "result": {
                        "place_id": "place-1",
                        "name": "Seoul",
                        "formatted_address": "Seoul, Republic of Korea",
                        "geometry": {"location": {"lat": 37.5, "lng": 127.0}},
                    },
                }
            ),
            FakeResponse(
                {
                    "status": "OK",
                    "results": [
                        {
                            "place_id": "place-1",
                            "name": "Seoul",
                            "formatted_address": "Seoul, Republic of Korea",
                            "geometry": {"location": {"lat": 37.5, "lng": 127.0}},
                        }
                    ],
                }
            ),
        )
        client = PlacesClient(api_key="test-google-value", db=self.db, session=session)
        self.assertEqual(client.autocomplete(query="Seo")[0]["place_id"], "place-1")
        self.assertEqual(client.details(place_id="place-1")["place_name"], "Seoul")
        self.assertEqual(client.search(query="Seoul")[0]["longitude"], 127.0)
        usage = self.db.query(CommonApiUsage).filter_by(api_name=ApiName.PLACES).one()
        self.assertEqual(usage.used_unit, 3)

        denied = PlacesClient(
            api_key="test-google-value",
            session=FakeHttpSession(FakeResponse({"status": "REQUEST_DENIED"})),
        )
        with self.assertRaises(ApiClientError):
            denied.search(query="Seoul")

    def test_places_nearby_uses_legacy_endpoint_and_normalizes_types(self):
        session = FakeHttpSession(
            FakeResponse(
                {
                    "status": "OK",
                    "results": [
                        {
                            "place_id": "valley-1",
                            "name": "피아골",
                            "vicinity": "전라남도 구례군 토지면",
                            "geometry": {
                                "location": {"lat": 35.23, "lng": 127.59}
                            },
                            "types": ["natural_feature", "point_of_interest"],
                            "business_status": "OPERATIONAL",
                        }
                    ],
                }
            )
        )
        client = PlacesClient(
            api_key="test-google-value",
            db=self.db,
            session=session,
        )
        item = client.nearby(
            latitude=35.2274,
            longitude=127.5905,
            radius_m=1500,
        )[0]
        self.assertEqual(item["place_name"], "피아골")
        self.assertEqual(item["types"], ["natural_feature", "point_of_interest"])
        request = session.requests[0]
        self.assertTrue(request["url"].endswith("/maps/api/place/nearbysearch/json"))
        self.assertEqual(request["params"]["radius"], "1500")
        usage = self.db.query(CommonApiUsage).filter_by(api_name=ApiName.PLACES).one()
        self.assertEqual(usage.used_unit, 1)

    def test_weather_current_forecast_no_key_provider_error_and_usage(self):
        current_payload = {
            "dt": 1_700_000_000,
            "main": {"temp": 10, "feels_like": 8, "humidity": 70, "pressure": 1012},
            "wind": {"speed": 2.5, "deg": 180},
            "clouds": {"all": 20},
            "weather": [{"id": 800, "description": "clear", "icon": "01d"}],
            "sys": {"sunrise": 1_699_980_000, "sunset": 1_700_020_000},
            "visibility": 10000,
            "name": "Seoul",
        }
        forecast_item = {
            "dt": 1_700_010_000,
            "main": {"temp": 9, "feels_like": 7, "humidity": 75, "pressure": 1010},
            "wind": {"speed": 3, "deg": 190},
            "clouds": {"all": 40},
            "weather": [{"id": 801, "description": "clouds", "icon": "02d"}],
            "pop": 0.2,
            "rain": {"3h": 0.5},
        }
        session = FakeHttpSession(
            FakeResponse(current_payload),
            FakeResponse({"list": [forecast_item]}),
        )
        client = WeatherClient(api_key="test-weather-value", db=self.db, session=session)
        self.assertEqual(client.get_weather(latitude=37.5, longitude=127)["temperature"], 10)
        self.assertEqual(client.get_forecast(latitude=37.5, longitude=127)[0]["rain_volume_mm"], 0.5)
        usage = self.db.query(CommonApiUsage).filter_by(api_name=ApiName.WEATHER).one()
        self.assertEqual(usage.used_unit, 2)

        with self.assertRaises(ApiClientError) as missing:
            WeatherClient(api_key="").get_weather(latitude=0, longitude=0)
        self.assertEqual(missing.exception.code, ExternalApiErrorCode.API_KEY_NOT_CONFIGURED)

        failed = WeatherClient(
            api_key="test-weather-value",
            session=FakeHttpSession(FakeResponse({}, status_code=500)),
        )
        failed.retry_count = 1
        with self.assertRaises(ApiClientError) as provider:
            failed.get_weather(latitude=0, longitude=0)
        self.assertEqual(provider.exception.code, ExternalApiErrorCode.PROVIDER_ERROR)

    def test_astrometry_submit_poll_complete_failed_timeout_and_no_key(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "image.jpg"
            image_path.write_bytes(b"image")
            session = FakeHttpSession(
                FakeResponse({"status": "success", "session": "provider-session"}),
                FakeResponse({"status": "success", "subid": 41}),
                FakeResponse({"jobs": [99]}),
                FakeResponse({"status": "success"}),
                FakeResponse(
                    {
                        "ra": 10,
                        "dec": 20,
                        "orientation": 30,
                        "pixscale": 2,
                        "radius": 1,
                        "parity": 1,
                    }
                ),
            )
            client = AstrometryClient(
                api_key="test-astrometry-value",
                db=self.db,
                session=session,
            )
            self.assertEqual(client.submit(image_path=str(image_path))["submission_id"], 41)
            completed = client.get_status(submission_id=41)
            self.assertEqual(completed["status"], "COMPLETED")
            self.assertEqual(completed["field_width"], 2)

        failed = AstrometryClient(
            api_key="test-astrometry-value",
            session=FakeHttpSession(
                FakeResponse({"jobs": [99]}),
                FakeResponse({"status": "failure"}),
            ),
        ).get_status(submission_id=41)
        self.assertEqual(failed["status"], "FAILED")

        waiting = AstrometryClient(
            api_key="test-astrometry-value",
            session=FakeHttpSession(FakeResponse({"jobs": []})),
        ).get_status(submission_id=41)
        self.assertEqual(waiting["status"], "WAITING")

        finished_without_job = AstrometryClient(
            api_key="test-astrometry-value",
            session=FakeHttpSession(
                FakeResponse(
                    {
                        "processing_finished": "2026-08-29 00:00:00.000000",
                        "jobs": [],
                    }
                )
            ),
        ).get_submission_status(submission_id=15936597)
        self.assertEqual(finished_without_job["status"], "WAITING")
        self.assertIsNone(finished_without_job["provider_job_id"])

        finished_with_job = AstrometryClient(
            api_key="test-astrometry-value",
            session=FakeHttpSession(
                FakeResponse(
                    {
                        "processing_started": "2026-08-29 00:00:00.000000",
                        "processing_finished": "2026-08-29 00:00:01.000000",
                        "user_images": [16194376],
                        "images": [39940594],
                        "jobs": [16772087],
                        "job_calibrations": [[16772087, 13358276]],
                    }
                )
            ),
        ).get_submission_status(submission_id=15936597)
        self.assertEqual(finished_with_job["status"], "PROCESSING")
        self.assertEqual(finished_with_job["provider_job_id"], 16772087)

        processing = AstrometryClient(
            api_key="test-astrometry-value",
            session=FakeHttpSession(
                FakeResponse({"jobs": [99]}),
                FakeResponse({"status": "solving"}),
            ),
        ).get_status(submission_id=41)
        self.assertEqual(processing["status"], "PROCESSING")

        timeout = AstrometryClient(
            api_key="test-astrometry-value",
            session=FakeHttpSession(requests.Timeout()),
        )
        with self.assertRaises(ApiClientError) as timed_out:
            timeout.get_status(submission_id=41)
        self.assertEqual(timed_out.exception.code, ExternalApiErrorCode.PROVIDER_TIMEOUT)

        missing_submission = AstrometryClient(
            api_key="test-astrometry-value",
            session=FakeHttpSession(FakeResponse({}, status_code=404)),
        )
        with self.assertRaises(AstrometryProviderWorkNotFound) as not_found:
            missing_submission.get_submission_status(submission_id=15936182)
        self.assertEqual(not_found.exception.resource, "submission")
        self.assertEqual(not_found.exception.provider_id, 15936182)

        with self.assertRaises(ApiClientError) as missing:
            AstrometryClient(api_key="").submit(image_path="missing.jpg")
        self.assertEqual(missing.exception.code, ExternalApiErrorCode.API_KEY_NOT_CONFIGURED)

    def test_plate_solve_legacy_opaque_job_get_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "image.jpg"
            image_path.write_bytes(b"image")
            common_file = CommonFile(
                file_id="f" * 64,
                original_name="image.jpg",
                original_path=str(image_path),
                width=3600,
                height=1800,
                service_name="AstroJournal",
            )
            self.db.add(common_file)
            self.db.flush()
            self.db.add(
                CommonFileService(file_id=common_file.id, service_name="AstroJournal")
            )
            self.db.commit()

            legacy_job_id = encrypt_value(
                json.dumps(
                    {
                        "v": 1,
                        "submission_id": 41,
                        "common_file_id": common_file.id,
                    },
                    separators=(",", ":"),
                )
            )
            self.assertNotIn("41", legacy_job_id)

            def legacy_status(*, submission_id: int):
                self.assertFalse(self.db.in_transaction())
                return {
                    "status": "COMPLETED",
                    "submission_id": submission_id,
                    "provider_job_id": 99,
                    "ra": 10,
                    "dec": 20,
                    "rotation": 30,
                    "pixel_scale": 2,
                    "field_width": 1,
                    "field_height": 1,
                    "parity": 1,
                }

            with patch(
                "app.astrojournal.services.plate_solve_service.AstrometryClient.get_status",
                side_effect=legacy_status,
            ):
                result = PlateSolveService(self.db).get(job_id=legacy_job_id)
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(result["result"]["field_width"], 2)
            self.assertEqual(result["result"]["field_height"], 1)

    def test_capabilities_openapi_and_configuration_error_contract(self):
        supported = capabilities()["capabilities"]
        self.assertTrue(supported["astro_records"])
        self.assertTrue(supported["geocoding"])
        self.assertTrue(supported["places"])
        self.assertTrue(supported["weather"])
        self.assertTrue(supported["plate_solve"])

        paths = app.openapi()["paths"]
        for path in (
            "/api/common/geocoding/reverse",
            "/api/common/geocoding/forward",
            "/api/common/places/autocomplete",
            "/api/common/places/details",
            "/api/common/places/search",
            "/api/common/weather/current",
            "/api/common/weather/forecast",
            "/api/astro/plate-solve",
            "/api/astro/plate-solve/summary",
            "/api/astro/plate-solve/{job_id}/retry",
            "/api/astro/plate-solve/{job_id}",
        ):
            self.assertIn(path, paths)

        with patch(
            "app.common.services.external_api_service.KeyResolver.resolve",
            return_value=None,
        ), self.assertRaises(HTTPException) as unavailable:
            current_weather(lat=0, lon=0, language="ko", db=self.db)
        self.assertEqual(unavailable.exception.status_code, 503)
        self.assertEqual(
            unavailable.exception.detail["code"],
            ExternalApiErrorCode.API_KEY_NOT_CONFIGURED.value,
        )
        with patch(
            "app.common.services.external_api_service.KeyResolver.resolve",
            return_value=None,
        ), self.assertRaises(ApiClientError) as geocoding_unavailable:
            ExternalApiService(self.db).forward_geocode(
                query="Seoul",
                language="ko",
            )
        self.assertEqual(
            geocoding_unavailable.exception.code,
            ExternalApiErrorCode.API_KEY_NOT_CONFIGURED,
        )

        with patch(
            "app.common.services.monitoring_service.KeyResolver.resolve_with_source",
            return_value=SimpleNamespace(source=KeySource.DATABASE),
        ):
            readiness = check_external_readiness(self.db)
        self.assertTrue(readiness["services"]["weather"]["configured"])
        self.assertEqual(readiness["services"]["weather"]["source"], "database")


if __name__ == "__main__":
    unittest.main()
