"""Central resolution of server-side external API credentials."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from app.common.config import Settings, settings
from app.common.models.api_key import ApiKey
from app.common.security.crypto import decrypt_value

logger = logging.getLogger(__name__)


class ExternalServiceName(StrEnum):
    GOOGLE_GEOCODING = "GOOGLE_GEOCODING"
    GOOGLE_PLACES = "GOOGLE_PLACES"
    WEATHER = "WEATHER"
    ASTROMETRY = "ASTROMETRY"


class KeySource(StrEnum):
    DATABASE = "database"
    ENVIRONMENT = "environment"


@dataclass(frozen=True)
class ResolvedApiKey:
    value: str
    source: KeySource


_ENVIRONMENT_FIELDS: dict[ExternalServiceName, str] = {
    ExternalServiceName.GOOGLE_GEOCODING: "GOOGLE_API_KEY",
    ExternalServiceName.GOOGLE_PLACES: "GOOGLE_API_KEY",
    ExternalServiceName.WEATHER: "WEATHER_API_KEY",
    ExternalServiceName.ASTROMETRY: "ASTROMETRY_API_KEY",
}


class KeyResolver:
    """Resolve an enabled encrypted DB key before its environment fallback."""

    def __init__(
        self,
        db: Session,
        *,
        settings_value: Settings | None = None,
    ) -> None:
        self.db = db
        self.settings = settings_value or settings

    def resolve(self, service_name: ExternalServiceName | str) -> str | None:
        resolved = self.resolve_with_source(service_name)
        return resolved.value if resolved is not None else None

    def resolve_with_source(
        self,
        service_name: ExternalServiceName | str,
    ) -> ResolvedApiKey | None:
        service = ExternalServiceName(service_name)
        item = (
            self.db.query(ApiKey)
            .filter(ApiKey.service_name == service.value)
            .filter(ApiKey.enabled.is_(True))
            .first()
        )
        if item is not None and item.api_key:
            try:
                value = decrypt_value(item.api_key).strip()
            except Exception as exc:
                logger.warning(
                    "API key decrypt failed service=%s error_type=%s",
                    service.value,
                    type(exc).__name__,
                )
            else:
                if value:
                    return ResolvedApiKey(value=value, source=KeySource.DATABASE)

        field_name = _ENVIRONMENT_FIELDS[service]
        fallback = getattr(self.settings, field_name, None)
        if fallback and str(fallback).strip():
            return ResolvedApiKey(
                value=str(fallback).strip(),
                source=KeySource.ENVIRONMENT,
            )
        return None

    def is_configured(self, service_name: ExternalServiceName | str) -> bool:
        return self.resolve_with_source(service_name) is not None

