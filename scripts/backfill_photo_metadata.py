"""Null-only EXIF and geography maintenance for existing photo assets.

The default mode is dry-run. Dry-run reads the database, original assets, and
the existing geocode cache only. It never calls an external provider and never
writes database, storage, Vision job, usage, or tag state.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.common.database import SessionLocal
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.repositories.geocode_cache_repository import GeocodeCacheRepository
from app.common.repositories.metadata_priority import MetadataPriority
from app.common.services.api_clients.google import GeocodingClient
from app.common.services.key_resolver import ExternalServiceName, KeyResolver
from app.common.services.photo_analysis import ExifReader
from app.common.services.storage_service import StorageService
from scripts.backfill_exif_metadata import (
    _apply_missing_values,
    _is_blank,
    _resolve_original_path,
)


PHOTO_EXIF_BACKFILL_FIELDS = (
    "camera_make",
    "camera_model",
    "lens",
    "datetime_original",
    "iso",
    "f_number",
    "exposure_time",
    "focal_length",
)
GEOGRAPHY_FIELDS = (
    "country",
    "province",
    "city",
    "district",
    "place_name",
)
Geocoder = Callable[[float, float], dict[str, Any]]


@dataclass
class PhotoMetadataBackfillStats:
    inspected_files: int = 0
    exif_backfill_targets: int = 0
    geography_backfill_targets: int = 0
    already_complete_skipped: int = 0
    no_source_values_skipped: int = 0
    missing_original_files: int = 0
    exif_extraction_failures: int = 0
    reverse_geocoding_failures: int = 0
    persistence_failures: int = 0
    would_update_files: int = 0
    updated_files: int = 0
    geocode_cache_hits: int = 0
    geocode_provider_calls: int = 0


def backfill_photo_metadata(
    db: Session,
    *,
    storage_service: StorageService,
    exif_reader: ExifReader | None = None,
    execute: bool = False,
    service_name: str = "MemoryKeeper",
    filename: str | None = None,
    limit: int | None = None,
    geocoder: Geocoder | None = None,
) -> PhotoMetadataBackfillStats:
    """Fill blank common EXIF/geography fields without altering asset identity."""
    reader = exif_reader or ExifReader()
    stats = PhotoMetadataBackfillStats()
    query = (
        db.query(CommonFile, CommonFileMetadata)
        .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
        .outerjoin(CommonFileMetadata, CommonFileMetadata.file_id == CommonFile.id)
        .filter(CommonFileService.service_name == service_name)
        .filter(CommonFile.deleted.is_(False))
        .order_by(CommonFile.id.asc())
    )
    if filename:
        query = query.filter(CommonFile.original_name == filename)
    if limit is not None:
        query = query.limit(limit)

    cache_repository = GeocodeCacheRepository(db)
    for common_file, metadata in query.all():
        stats.inspected_files += 1
        exif_values: dict[str, Any] = {}
        geography_values: dict[str, Any] = {}
        needs_exif_read = _has_blank_exif_field(metadata)

        if needs_exif_read:
            stored_path = common_file.original_path
            if not stored_path:
                stats.missing_original_files += 1
            else:
                try:
                    original_path = _resolve_original_path(storage_service, stored_path)
                    if not original_path.is_file():
                        stats.missing_original_files += 1
                    else:
                        extracted = reader.read(original_path)
                        if not extracted:
                            stats.exif_extraction_failures += 1
                        else:
                            exif_values = _select_missing_exif_values(
                                metadata,
                                extracted,
                            )
                except Exception:
                    stats.exif_extraction_failures += 1

        if exif_values:
            stats.exif_backfill_targets += 1

        latitude = _metadata_number(metadata, "gps_lat")
        longitude = _metadata_number(metadata, "gps_lon")
        geography_missing = _missing_fields(metadata, GEOGRAPHY_FIELDS)
        geography_target = (
            _valid_coordinates(latitude, longitude) and bool(geography_missing)
        )
        if geography_target:
            stats.geography_backfill_targets += 1
            cached = cache_repository.find(latitude=latitude, longitude=longitude)
            if cached is not None:
                stats.geocode_cache_hits += 1
                geography_values = _select_blank_values(
                    metadata,
                    {field: getattr(cached, field) for field in GEOGRAPHY_FIELDS},
                )
            cache_covers_gaps = all(
                field_name in geography_values for field_name in geography_missing
            )
            if execute and not cache_covers_gaps:
                try:
                    result = _reverse_geocode(
                        db,
                        latitude=latitude,
                        longitude=longitude,
                        geocoder=geocoder,
                    )
                    stats.geocode_provider_calls += 1
                    provider_values = _select_blank_values(metadata, result)
                    geography_values.update(provider_values)
                    if provider_values:
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
                except Exception:
                    db.rollback()
                    stats.reverse_geocoding_failures += 1

        if not exif_values and not geography_target:
            if needs_exif_read:
                stats.no_source_values_skipped += 1
            else:
                stats.already_complete_skipped += 1
            continue

        if not execute:
            stats.would_update_files += 1
            continue

        changed = False
        try:
            if exif_values:
                changed = _apply_missing_values(
                    db,
                    common_file_id=common_file.id,
                    values=exif_values,
                    source="EXIF",
                    priority=MetadataPriority.EXIF,
                    modified_by="backfill_photo_metadata.py",
                ) or changed
            if geography_values:
                changed = _apply_missing_values(
                    db,
                    common_file_id=common_file.id,
                    values=geography_values,
                    source="GPS",
                    priority=MetadataPriority.GPS,
                    modified_by="backfill_photo_metadata.py",
                ) or changed
        except Exception:
            db.rollback()
            stats.persistence_failures += 1
            continue
        if changed:
            stats.updated_files += 1

    return stats


def _has_blank_exif_field(metadata: CommonFileMetadata | None) -> bool:
    return metadata is None or any(
        _is_blank(getattr(metadata, field_name, None))
        for field_name in PHOTO_EXIF_BACKFILL_FIELDS
    )


def _select_missing_exif_values(
    metadata: CommonFileMetadata | None,
    extracted: dict[str, Any],
) -> dict[str, Any]:
    return {
        field_name: extracted.get(field_name)
        for field_name in PHOTO_EXIF_BACKFILL_FIELDS
        if (metadata is None or _is_blank(getattr(metadata, field_name, None)))
        and not _is_blank(extracted.get(field_name))
    }


def _missing_fields(
    metadata: CommonFileMetadata | None,
    fields: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        field_name
        for field_name in fields
        if metadata is None or _is_blank(getattr(metadata, field_name, None))
    )


def _select_blank_values(
    metadata: CommonFileMetadata | None,
    values: dict[str, Any],
) -> dict[str, Any]:
    return {
        field_name: values.get(field_name)
        for field_name in GEOGRAPHY_FIELDS
        if (metadata is None or _is_blank(getattr(metadata, field_name, None)))
        and not _is_blank(values.get(field_name))
    }


def _metadata_number(
    metadata: CommonFileMetadata | None,
    field_name: str,
) -> float | None:
    value = getattr(metadata, field_name, None) if metadata is not None else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _valid_coordinates(latitude: float | None, longitude: float | None) -> bool:
    return (
        latitude is not None
        and longitude is not None
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


def _reverse_geocode(
    db: Session,
    *,
    latitude: float,
    longitude: float,
    geocoder: Geocoder | None,
) -> dict[str, Any]:
    if geocoder is not None:
        return geocoder(latitude, longitude)
    api_key = KeyResolver(db).resolve(ExternalServiceName.GOOGLE_GEOCODING)
    if not api_key:
        raise RuntimeError("Google Geocoding credential is not configured")
    return GeocodingClient(api_key=api_key, db=db).reverse_geocode(
        latitude=latitude,
        longitude=longitude,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill blank EXIF and geography metadata from originals/GPS",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read/report only (default)")
    mode.add_argument(
        "--execute",
        "--apply",
        dest="execute",
        action="store_true",
        help="Persist null-only updates and allow reverse-geocoding cache misses",
    )
    parser.add_argument("--service", default="MemoryKeeper")
    parser.add_argument("--filename", default=None)
    parser.add_argument("--limit", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")

    db = SessionLocal()
    try:
        stats = backfill_photo_metadata(
            db,
            storage_service=StorageService(),
            execute=bool(args.execute),
            service_name=args.service,
            filename=args.filename,
            limit=args.limit,
        )
    finally:
        db.close()

    summary = asdict(stats)
    summary["mode"] = "execute" if args.execute else "dry-run"
    summary["service"] = args.service
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    failures = (
        stats.exif_extraction_failures
        + stats.reverse_geocoding_failures
        + stats.persistence_failures
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
