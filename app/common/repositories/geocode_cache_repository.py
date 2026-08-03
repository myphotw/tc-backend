from __future__ import annotations

from sqlalchemy.orm import Session

from app.common.models.geocode_cache import CommonGeocodeCache


class GeocodeCacheRepository:
    """common_geocode_cache 저장소."""

    PRECISION = 5

    def __init__(self, db: Session) -> None:
        self.db = db

    @classmethod
    def normalize_coordinate(cls, value: float) -> float:
        """GPS 좌표를 소수점 5자리로 반올림한다."""
        return round(float(value), cls.PRECISION)

    def find(
        self,
        *,
        latitude: float,
        longitude: float,
    ) -> CommonGeocodeCache | None:
        """정규화된 좌표로 캐시를 조회한다."""
        lat = self.normalize_coordinate(latitude)
        lon = self.normalize_coordinate(longitude)
        return (
            self.db.query(CommonGeocodeCache)
            .filter(CommonGeocodeCache.deleted.is_(False))
            .filter(CommonGeocodeCache.latitude == lat)
            .filter(CommonGeocodeCache.longitude == lon)
            .first()
        )

    def exists(self, *, latitude: float, longitude: float) -> bool:
        """캐시 존재 여부를 반환한다."""
        return self.find(latitude=latitude, longitude=longitude) is not None

    def save(
        self,
        *,
        latitude: float,
        longitude: float,
        country: str | None = None,
        province: str | None = None,
        city: str | None = None,
        district: str | None = None,
        place_name: str | None = None,
        provider: str = "GOOGLE",
    ) -> CommonGeocodeCache:
        """캐시를 저장하거나 동일 좌표가 있으면 갱신한다."""
        lat = self.normalize_coordinate(latitude)
        lon = self.normalize_coordinate(longitude)
        item = self.find(latitude=lat, longitude=lon)
        if item is None:
            item = CommonGeocodeCache(
                latitude=lat,
                longitude=lon,
                country=country,
                province=province,
                city=city,
                district=district,
                place_name=place_name,
                provider=provider,
                deleted=False,
            )
            self.db.add(item)
        else:
            item.country = country
            item.province = province
            item.city = city
            item.district = district
            item.place_name = place_name
            item.provider = provider
            item.deleted = False

        self.db.commit()
        self.db.refresh(item)
        return item
