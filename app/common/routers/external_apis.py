"""Normalized public endpoints for server-side external API access."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.common.database import get_db
from app.common.schemas.external_api import (
    LocationCandidate,
    LocationCandidateList,
    PlacesAutocompleteItem,
    PlacesAutocompleteResponse,
    ReverseGeocodeResponse,
    WeatherCurrentResponse,
    WeatherForecastResponse,
)
from app.common.services.api_clients.base_client import (
    ApiClientError,
    ExternalApiErrorCode,
)
from app.common.services.external_api_service import ExternalApiService

router = APIRouter(prefix="/api/common", tags=["External APIs"])


@router.get("/geocoding/reverse", response_model=ReverseGeocodeResponse)
def reverse_geocode(
    latitude: float = Query(ge=-90, le=90),
    longitude: float = Query(ge=-180, le=180),
    language: str = Query(default="ko", min_length=2, max_length=10),
    db: Session = Depends(get_db),
):
    return _call(
        ExternalApiService(db).reverse_geocode,
        latitude=latitude,
        longitude=longitude,
        language=language,
    )


@router.get("/geocoding/forward", response_model=LocationCandidateList)
def forward_geocode(
    query: str = Query(min_length=1, max_length=300),
    language: str = Query(default="ko", min_length=2, max_length=10),
    db: Session = Depends(get_db),
):
    items = _call(
        ExternalApiService(db).forward_geocode,
        query=query.strip(),
        language=language,
    )
    return LocationCandidateList(items=[LocationCandidate(**item) for item in items])


@router.get("/places/autocomplete", response_model=PlacesAutocompleteResponse)
def places_autocomplete(
    query: str = Query(min_length=1, max_length=300),
    language: str = Query(default="ko", min_length=2, max_length=10),
    session_token: str | None = Query(default=None, min_length=8, max_length=128),
    db: Session = Depends(get_db),
):
    items = _call(
        ExternalApiService(db).places_autocomplete,
        query=query.strip(),
        language=language,
        session_token=session_token,
    )
    return PlacesAutocompleteResponse(
        items=[PlacesAutocompleteItem(**item) for item in items],
        session_token=session_token,
    )


@router.get("/places/details", response_model=LocationCandidate)
def place_details(
    place_id: str = Query(min_length=1, max_length=300),
    language: str = Query(default="ko", min_length=2, max_length=10),
    session_token: str | None = Query(default=None, min_length=8, max_length=128),
    db: Session = Depends(get_db),
):
    return _call(
        ExternalApiService(db).place_details,
        place_id=place_id,
        language=language,
        session_token=session_token,
    )


@router.get("/places/search", response_model=LocationCandidateList)
def places_search(
    query: str = Query(min_length=1, max_length=300),
    language: str = Query(default="ko", min_length=2, max_length=10),
    db: Session = Depends(get_db),
):
    items = _call(
        ExternalApiService(db).places_search,
        query=query.strip(),
        language=language,
    )
    return LocationCandidateList(items=[LocationCandidate(**item) for item in items])


@router.get("/weather/current", response_model=WeatherCurrentResponse)
def current_weather(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    language: str = Query(default="ko", min_length=2, max_length=10),
    db: Session = Depends(get_db),
):
    return _call(
        ExternalApiService(db).current_weather,
        latitude=lat,
        longitude=lon,
        language=language,
    )


@router.get("/weather/forecast", response_model=WeatherForecastResponse)
def weather_forecast(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    language: str = Query(default="ko", min_length=2, max_length=10),
    db: Session = Depends(get_db),
):
    items = _call(
        ExternalApiService(db).forecast,
        latitude=lat,
        longitude=lon,
        language=language,
    )
    return WeatherForecastResponse(items=items)


def _call(function, **kwargs):
    try:
        return function(**kwargs)
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
