from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.config import settings
from app.common.database import Base
from app.common.models.api_usage import CommonApiUsage
from app.common.repositories.api_usage_repository import (
    ApiName,
    ApiProvider,
    ApiUsageRepository,
)


class ApiUsageDefaultLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.repository = ApiUsageRepository(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_platesolve_effective_limit_uses_default_setting(self) -> None:
        with patch.object(settings, "PLATESOLVE_MONTHLY_LIMIT", 12345):
            self.assertEqual(
                ApiUsageRepository.effective_limit(ApiName.PLATESOLVE),
                12345,
            )

    def test_platesolve_first_unit_reservation_uses_default_setting(self) -> None:
        with patch.object(settings, "PLATESOLVE_MONTHLY_LIMIT", 100000):
            reserved = self.repository.reserve_usage(
                provider=ApiProvider.ASTROMETRY,
                api_name=ApiName.PLATESOLVE,
            )

        usage = self.db.query(CommonApiUsage).one()
        self.assertTrue(reserved)
        self.assertEqual(usage.limit_unit, 100000)
        self.assertEqual(usage.used_unit, 1)
        self.assertEqual(usage.remaining_unit, 99999)

    def test_nonvision_effective_limits_use_their_default_settings(self) -> None:
        with patch.object(settings, "GEOCODING_MONTHLY_LIMIT", 111), patch.object(
            settings,
            "WEATHER_MONTHLY_LIMIT",
            222,
        ):
            self.assertEqual(
                ApiUsageRepository.effective_limit(ApiName.GEOCODING),
                111,
            )
            self.assertEqual(
                ApiUsageRepository.effective_limit(ApiName.PLACES),
                111,
            )
            self.assertEqual(
                ApiUsageRepository.effective_limit(ApiName.WEATHER),
                222,
            )

    def test_vision_default_still_has_900_safe_cap(self) -> None:
        with patch.object(settings, "VISION_MONTHLY_LIMIT", 2000):
            self.assertEqual(
                ApiUsageRepository.effective_limit(ApiName.VISION),
                900,
            )

    def test_explicit_configured_limit_keeps_existing_meaning(self) -> None:
        self.assertEqual(
            ApiUsageRepository.effective_limit(ApiName.PLATESOLVE, configured=7),
            7,
        )
        self.assertEqual(
            ApiUsageRepository.effective_limit(ApiName.VISION, configured=1200),
            900,
        )
        self.assertEqual(
            ApiUsageRepository.effective_limit(ApiName.VISION, configured=500),
            500,
        )


if __name__ == "__main__":
    unittest.main()
