from __future__ import annotations

from datetime import datetime, timezone

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


class ApiUsageRepository:
    """common_api_usage 저장소."""

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

        Vision Worker는 이 메서드로 월 1000 Unit 정책을 확인할 수 있다.
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
        return int(mapping.get(api_name, 0))
