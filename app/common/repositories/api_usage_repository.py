from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.common.config import settings
from app.common.models.api_usage import CommonApiUsage


class ApiProvider:
    """API Usage provider 값."""

    GOOGLE = "GOOGLE"
    WEATHER = "WEATHER"
    ASTROMETRY = "ASTROMETRY"
    LOCAL = "LOCAL"


class ApiName:
    """API Usage api_name 값."""

    VISION = "VISION"
    GEOCODING = "GEOCODING"
    PLACES = "PLACES"
    WEATHER = "WEATHER"
    PLATESOLVE = "PLATESOLVE"


class ApiUsageLimitExceeded(RuntimeError):
    """The configured/effective monthly usage limit has no reservable units."""


class ApiUsageRepository:
    """common_api_usage 저장소."""

    VISION_FREE_TIER_LIMIT = 1000
    VISION_SAFE_MONTHLY_LIMIT = 900

    def __init__(self, db: Session) -> None:
        self.db = db

    def increase_usage(
        self,
        *,
        provider: str,
        api_name: str,
        units: int = 1,
        year: int | None = None,
        month: int | None = None,
    ) -> CommonApiUsage:
        """API 사용량을 증가시킨다. 현재는 Mock 증가만 수행한다."""
        if provider == ApiProvider.GOOGLE and api_name == ApiName.VISION:
            if not self.reserve_usage(
                provider=provider,
                api_name=api_name,
                units=units,
                year=year,
                month=month,
            ):
                raise ApiUsageLimitExceeded("VISION monthly safe limit reached")
            return self.get_usage(
                provider=provider,
                api_name=api_name,
                year=year,
                month=month,
            )
        usage = self._get_or_create(
            provider=provider,
            api_name=api_name,
            year=year,
            month=month,
        )
        usage.used_unit = (usage.used_unit or 0) + units
        usage.remaining_unit = max(0, usage.limit_unit - usage.used_unit)
        usage.last_called_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(usage)
        return usage

    def reserve_usage(
        self,
        *,
        provider: str,
        api_name: str,
        units: int = 1,
        year: int | None = None,
        month: int | None = None,
    ) -> bool:
        """Atomically reserve units before an external provider call.

        The conditional UPDATE is the concurrency boundary. Multiple workers
        may observe remaining quota, but only reservations whose resulting
        ``used_unit`` is within the effective limit can commit.
        """
        if units < 1:
            raise ValueError("units must be at least 1")
        now = datetime.now(timezone.utc)
        target_year = year if year is not None else now.year
        target_month = month if month is not None else now.month
        limit_unit = self.effective_limit(api_name)
        if units > limit_unit:
            return False

        values = {
            "provider": provider,
            "api_name": api_name,
            "year": target_year,
            "month": target_month,
            "used_unit": 0,
            "limit_unit": limit_unit,
            "remaining_unit": limit_unit,
            "deleted": False,
        }
        dialect = self.db.get_bind().dialect.name
        if dialect == "postgresql":
            create = postgresql_insert(CommonApiUsage).values(**values)
            create = create.on_conflict_do_nothing(
                index_elements=["provider", "api_name", "year", "month"]
            )
        elif dialect == "sqlite":
            create = sqlite_insert(CommonApiUsage).values(**values)
            create = create.on_conflict_do_nothing(
                index_elements=["provider", "api_name", "year", "month"]
            )
        else:
            # TC-Backend production/test dialects are PostgreSQL and SQLite.
            # A generic dialect must already have its monthly row initialized.
            create = None
        if create is not None:
            self.db.execute(create)

        reserve = (
            update(CommonApiUsage)
            .where(CommonApiUsage.provider == provider)
            .where(CommonApiUsage.api_name == api_name)
            .where(CommonApiUsage.year == target_year)
            .where(CommonApiUsage.month == target_month)
            .where(CommonApiUsage.used_unit + units <= limit_unit)
            .values(
                used_unit=CommonApiUsage.used_unit + units,
                limit_unit=limit_unit,
                remaining_unit=limit_unit - (CommonApiUsage.used_unit + units),
                last_called_at=now,
                deleted=False,
            )
        )
        result = self.db.execute(reserve)
        reserved = result.rowcount == 1
        self.db.commit()
        return reserved

    def get_usage(
        self,
        *,
        provider: str,
        api_name: str,
        year: int | None = None,
        month: int | None = None,
    ) -> CommonApiUsage:
        """월별 사용량 Row를 조회하거나 생성한다."""
        return self._get_or_create(
            provider=provider,
            api_name=api_name,
            year=year,
            month=month,
        )

    def can_use(
        self,
        *,
        provider: str,
        api_name: str,
        units: int = 1,
        year: int | None = None,
        month: int | None = None,
    ) -> bool:
        """
        사용 가능 여부를 반환한다.

        Vision Worker는 이 메서드를 advisory pre-check로 사용한다. 실제
        hard-cap 경계는 reserve_usage()의 원자적 조건부 UPDATE다.
        """
        return self.remaining(
            provider=provider,
            api_name=api_name,
            year=year,
            month=month,
        ) >= units

    def remaining(
        self,
        *,
        provider: str,
        api_name: str,
        year: int | None = None,
        month: int | None = None,
    ) -> int:
        """남은 Unit을 반환한다."""
        usage = self.get_usage(
            provider=provider,
            api_name=api_name,
            year=year,
            month=month,
        )
        return max(0, usage.limit_unit - usage.used_unit)

    def _get_or_create(
        self,
        *,
        provider: str,
        api_name: str,
        year: int | None = None,
        month: int | None = None,
    ) -> CommonApiUsage:
        """월별 Usage Row를 조회하거나 생성한다."""
        now = datetime.now(timezone.utc)
        target_year = year if year is not None else now.year
        target_month = month if month is not None else now.month
        limit_unit = self._default_limit(api_name)

        usage = (
            self.db.query(CommonApiUsage)
            .filter(CommonApiUsage.deleted.is_(False))
            .filter(CommonApiUsage.provider == provider)
            .filter(CommonApiUsage.api_name == api_name)
            .filter(CommonApiUsage.year == target_year)
            .filter(CommonApiUsage.month == target_month)
            .first()
        )
        if usage is not None:
            if usage.limit_unit != limit_unit:
                usage.limit_unit = limit_unit
                usage.remaining_unit = max(0, limit_unit - usage.used_unit)
                self.db.commit()
                self.db.refresh(usage)
            return usage

        usage = CommonApiUsage(
            provider=provider,
            api_name=api_name,
            year=target_year,
            month=target_month,
            used_unit=0,
            limit_unit=limit_unit,
            remaining_unit=limit_unit,
            deleted=False,
        )
        self.db.add(usage)
        self.db.commit()
        self.db.refresh(usage)
        return usage

    def _default_limit(self, api_name: str) -> int:
        """Config 기반 월간 limit을 반환한다."""
        mapping = {
            ApiName.VISION: settings.VISION_MONTHLY_LIMIT,
            ApiName.GEOCODING: settings.GEOCODING_MONTHLY_LIMIT,
            ApiName.PLACES: settings.GEOCODING_MONTHLY_LIMIT,
            ApiName.WEATHER: settings.WEATHER_MONTHLY_LIMIT,
            ApiName.PLATESOLVE: settings.PLATESOLVE_MONTHLY_LIMIT,
        }
        return self.effective_limit(api_name, configured=mapping.get(api_name, 0))

    @classmethod
    def effective_limit(
        cls,
        api_name: str,
        *,
        configured: int | None = None,
    ) -> int:
        configured_limit = (
            int(settings.VISION_MONTHLY_LIMIT)
            if configured is None and api_name == ApiName.VISION
            else int(configured or 0)
        )
        configured_limit = max(0, configured_limit)
        if api_name == ApiName.VISION:
            return min(configured_limit, cls.VISION_SAFE_MONTHLY_LIMIT)
        return configured_limit
