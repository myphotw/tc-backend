from __future__ import annotations

import time

from app.common.repositories.geocode_cache_repository import GeocodeCacheRepository
from app.common.repositories.metadata_repository import (
    MetadataRepository,
    MetadataSource,
)
from app.common.services.api_clients.google import GeocodingClient
from app.common.services.key_resolver import ExternalServiceName, KeyResolver
from app.common.utils.perf import elapsed_ms, log_perf
from worker.plugins.base import BasePlugin, PluginContext


class GpsPlugin(BasePlugin):
    """
    EXIF GPS 좌표를 Cache / GeocodingClient로 주소 변환하고 Metadata에 저장한다.
    """

    plugin_name = "GpsPlugin"
    plugin_version = "1.0.0"
    plugin_priority = 60
    worker_scope = "upload"

    def run(self, context: PluginContext) -> None:
        if not context.has_gps:
            context.log("GPS_SKIPPED")
            return

        if context.common_file is None:
            context.log("GPS_FAILED:common_file is required")
            return

        if context.gps_lat is None or context.gps_lon is None:
            context.log("GPS_SKIPPED")
            return

        try:
            latitude = float(context.gps_lat)
            longitude = float(context.gps_lon)
            cache_repository = GeocodeCacheRepository(context.db)
            cached = cache_repository.find(latitude=latitude, longitude=longitude)

            if cached is not None:
                result = {
                    "country": cached.country,
                    "province": cached.province,
                    "city": cached.city,
                    "district": cached.district,
                    "place_name": cached.place_name,
                    "latitude": cached.latitude,
                    "longitude": cached.longitude,
                    "source": "cache",
                }
                context.log("GPS_CACHE_HIT")
                log_perf(
                    "geocoding",
                    cache="hit",
                    external_api_ms=0,
                    job_id=getattr(context.job, "job_id", None),
                )
            else:
                api_key = KeyResolver(context.db).resolve(
                    ExternalServiceName.GOOGLE_GEOCODING
                )
                client = GeocodingClient(api_key=api_key, db=context.db)
                api_started = time.perf_counter()
                result = client.reverse_geocode(
                    latitude=latitude,
                    longitude=longitude,
                )
                api_ms = elapsed_ms(api_started)
                cache_repository.save(
                    latitude=latitude,
                    longitude=longitude,
                    country=result.get("country"),
                    province=result.get("province"),
                    city=result.get("city"),
                    district=result.get("district"),
                    place_name=result.get("place_name"),
                    provider="GOOGLE",
                )
                context.log("GPS_CACHE_MISS")
                log_perf(
                    "geocoding",
                    cache="miss",
                    external_api_ms=api_ms,
                    job_id=getattr(context.job, "job_id", None),
                )

            metadata = {
                "country": result.get("country"),
                "province": result.get("province"),
                "city": result.get("city"),
                "district": result.get("district"),
                "place_name": result.get("place_name"),
                "gps_lat": result.get("latitude", latitude),
                "gps_lon": result.get("longitude", longitude),
            }
            MetadataRepository(context.db).upsert_fields(
                file_id=context.common_file.id,
                values=metadata,
                source=MetadataSource.GPS,
                modified_by="GpsPlugin",
            )

            context.resolved_country = metadata.get("country")
            context.resolved_province = metadata.get("province")
            context.resolved_city = metadata.get("city")
            context.resolved_district = metadata.get("district")
            context.resolved_place = metadata.get("place_name")
            context.metadata.update(
                {
                    key: value
                    for key, value in metadata.items()
                    if value is not None
                }
            )
            context.log("GPS_COMPLETE")
        except Exception as exc:
            context.log(f"GPS_FAILED:{exc}")
