from __future__ import annotations

from app.common.repositories.api_usage_repository import (
    ApiName,
    ApiProvider,
    ApiUsageRepository,
)
from app.common.repositories.geocode_cache_repository import GeocodeCacheRepository
from app.common.repositories.metadata_repository import (
    MetadataRepository,
    MetadataSource,
)
from app.common.services.api_clients.google import GeocodingClient
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
            else:
                usage_repository = ApiUsageRepository(context.db)
                if not usage_repository.can_use(
                    provider=ApiProvider.GOOGLE,
                    api_name=ApiName.GEOCODING,
                    units=1,
                ):
                    context.log("GPS_FAILED:GEOCODING usage limit exceeded")
                    return

                client = GeocodingClient(db=context.db)
                result = client.reverse_geocode(
                    latitude=latitude,
                    longitude=longitude,
                )
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

            metadata = {
                "country": result.get("country"),
                "province": result.get("province"),
                "city": result.get("city"),
                "district": result.get("district"),
                "place_name": result.get("place_name"),
                "gps_lat": result.get("latitude", latitude),
                "gps_lon": result.get("longitude", longitude),
            }
            MetadataRepository(context.db).save_metadata(
                file_id=context.common_file.id,
                metadata=metadata,
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
