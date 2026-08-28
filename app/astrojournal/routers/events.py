"""AstroJournal normalized astronomy event API."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from app.astrojournal.schemas.astronomy_event import AstronomyEventListResponse
from app.astrojournal.services.astronomy_event_service import AstronomyEventService
from app.common.services.api_clients.base_client import (
    ApiClientError,
    ExternalApiErrorCode,
)

router = APIRouter(prefix="/api/astro/events", tags=["AstroJournal Events"])


@router.get("", response_model=AstronomyEventListResponse)
def list_astronomy_events(
    from_at: datetime | None = Query(default=None, alias="from"),
    to_at: datetime | None = Query(default=None, alias="to"),
) -> AstronomyEventListResponse:
    try:
        return AstronomyEventService().list_events(
            from_at=from_at,
            to_at=to_at,
        )
    except ApiClientError as exc:
        status_by_code = {
            ExternalApiErrorCode.API_KEY_NOT_CONFIGURED: 503,
            ExternalApiErrorCode.API_LIMIT_EXCEEDED: 429,
            ExternalApiErrorCode.PROVIDER_TIMEOUT: 504,
            ExternalApiErrorCode.PROVIDER_ERROR: 502,
            ExternalApiErrorCode.INVALID_REQUEST: 400,
        }
        raise HTTPException(
            status_code=status_by_code[exc.code],
            detail={"code": exc.code.value, "message": str(exc)},
        ) from exc
