from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LocationCandidate(BaseModel):
    display_name: str
    latitude: float
    longitude: float
    country: str | None = None
    province: str | None = None
    city: str | None = None
    district: str | None = None
    place_name: str | None = None
    provider: str
    place_id: str | None = None


class ReverseGeocodeResponse(BaseModel):
    display_name: str | None = None
    latitude: float
    longitude: float
    country: str | None = None
    province: str | None = None
    city: str | None = None
    district: str | None = None
    place_name: str | None = None
    provider: str
    source: str


class LocationCandidateList(BaseModel):
    items: list[LocationCandidate]


class PlacesAutocompleteItem(BaseModel):
    place_id: str
    main_text: str
    secondary_text: str | None = None
    display_name: str


class PlacesAutocompleteResponse(BaseModel):
    items: list[PlacesAutocompleteItem]
    session_token: str | None = None


class WeatherCurrentResponse(BaseModel):
    provider: str
    temperature: float | None = None
    feels_like: float | None = None
    humidity: int | None = None
    pressure: int | None = None
    clouds: int | None = None
    wind_speed: float | None = None
    wind_direction: int | None = None
    weather_code: int | None = None
    description: str | None = None
    icon: str | None = None
    visibility: int | None = None
    city_name: str | None = None
    observed_at: str | None = None
    sunrise: str | None = None
    sunset: str | None = None


class WeatherForecastItem(BaseModel):
    timestamp: str | None = None
    temperature: float | None = None
    feels_like: float | None = None
    humidity: int | None = None
    pressure: int | None = None
    clouds: int | None = None
    wind_speed: float | None = None
    wind_direction: int | None = None
    weather_code: int | None = None
    description: str | None = None
    icon: str | None = None
    visibility: int | None = None
    precipitation_probability: float | None = None
    rain_volume_mm: float | None = None


class WeatherForecastResponse(BaseModel):
    provider: str = "openweathermap"
    items: list[WeatherForecastItem]


class PlateSolveCreateRequest(BaseModel):
    common_file_id: int = Field(gt=0)


class SipDistortion(BaseModel):
    a_order: int | None = None
    b_order: int | None = None
    ap_order: int | None = None
    bp_order: int | None = None
    a: dict[str, float] = Field(default_factory=dict)
    b: dict[str, float] = Field(default_factory=dict)
    ap: dict[str, float] = Field(default_factory=dict)
    bp: dict[str, float] = Field(default_factory=dict)


class CanonicalWcs(BaseModel):
    schema_version: Literal[1] = 1
    ctype1: str
    ctype2: str
    cunit1: str | None = None
    cunit2: str | None = None
    radesys: str | None = None
    equinox: float | None = None
    lonpole: float | None = None
    latpole: float | None = None
    crval1: float
    crval2: float
    crpix1: float
    crpix2: float
    cd11: float
    cd12: float
    cd21: float
    cd22: float
    raster_width: int
    raster_height: int
    sip: SipDistortion | None = None


class PlateSolveResult(BaseModel):
    ra: float | None = None
    dec: float | None = None
    rotation: float | None = None
    pixel_scale: float | None = None
    field_width: float | None = None
    field_height: float | None = None
    parity: float | None = None
    image_width: int | None = None
    image_height: int | None = None
    wcs: CanonicalWcs | None = None


class PlateSolveJobResponse(BaseModel):
    job_id: str
    status: str
    common_file_id: int
    provider: str = "astrometry.net"
    result: PlateSolveResult | None = None
    provider_metadata: dict[str, int | str | None] = Field(default_factory=dict)


class PlateSolveStatusSummary(BaseModel):
    total: int
    WAITING: int
    PROCESSING: int
    COMPLETED: int
    FAILED: int
