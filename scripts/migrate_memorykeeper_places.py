"""Import a JSON export of legacy TB_PLACE rows; default mode is dry-run."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.common.database import SessionLocal
from app.common.repositories.change_event_repository import ChangeEventRepository, ChangeOperation
from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.repositories.place_repository import MemoryKeeperPlaceRepository
from app.memorykeeper.schemas.place import PlaceCreate


@dataclass
class MigrationStats:
    scanned: int = 0
    created: int = 0
    unchanged: int = 0
    failed: int = 0


def migrate_rows(db: Session, rows: list[dict[str, object]], *, execute: bool = False) -> MigrationStats:
    stats = MigrationStats()
    repository = MemoryKeeperPlaceRepository(db)
    for row in rows:
        stats.scanned += 1
        try:
            raw_legacy_id = str(_value(row, "Id", "id") or "").strip()
            if not raw_legacy_id:
                raise ValueError("legacy Id is required")
            legacy_id = str(UUID(raw_legacy_id))
            if repository.get(legacy_id, include_deleted=True) is not None:
                stats.unchanged += 1
                continue
            payload = PlaceCreate(
                display_name=_value(row, "DisplayName", "display_name"),
                canonical_name=_value(row, "CanonicalName", "canonical_name"),
                address=_value(row, "Address", "address"),
                postal_code=_value(row, "PostalCode", "postal_code"),
                country=_value(row, "Country", "country"),
                province=_value(row, "Province", "province"),
                city=_value(row, "City", "city"),
                district=_value(row, "District", "district"),
                latitude=_value(row, "Latitude", "latitude"),
                longitude=_value(row, "Longitude", "longitude"),
                radius_m=_value(row, "Radius", "radius_m") or 100.0,
                provider_place_id=_value(row, "GooglePlaceId", "provider_place_id"),
                category=_value(row, "Category", "category"),
                active=_bool_value(row, "IsActive", "active", default=True),
                favorite=_bool_value(row, "IsFavorite", "favorite", default=False),
            )
            stats.created += 1
            if execute:
                place = MemoryKeeperPlace(
                    id=legacy_id,
                    **payload.model_dump(),
                    usage_count=int(_value(row, "UsageCount", "usage_count") or 0),
                    last_used_at=_datetime_value(row, "LastUsedAt", "last_used_at"),
                    created_at=_datetime_value(row, "CreatedAt", "created_at"),
                    updated_at=_datetime_value(row, "UpdatedAt", "updated_at"),
                )
                repository.create(place)
                ChangeEventRepository(db).append(
                    service_name="MemoryKeeper",
                    resource_type="MemoryKeeperPlace",
                    resource_id=place.id,
                    operation=ChangeOperation.CREATE,
                    revision=place.revision,
                )
                db.commit()
        except (ValueError, TypeError, ValidationError):
            if execute:
                db.rollback()
            stats.failed += 1
    return stats


def _value(row: dict[str, object], *names: str):
    for name in names:
        if name in row:
            return row[name]
    return None


def _bool_value(row: dict[str, object], *names: str, default: bool) -> bool:
    value = _value(row, *names)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes"}
    return bool(value)


def _datetime_value(row: dict[str, object], *names: str) -> datetime | None:
    value = _value(row, *names)
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import legacy MemoryKeeper TB_PLACE JSON")
    parser.add_argument("--input", required=True, type=Path, help="JSON array exported from TB_PLACE")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Validate/report only (default)")
    mode.add_argument("--execute", action="store_true", help="Persist imported places")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise SystemExit("input must be a JSON array")
    db = SessionLocal()
    try:
        stats = migrate_rows(db, rows, execute=bool(args.execute))
    finally:
        db.close()
    summary = asdict(stats)
    summary["mode"] = "execute" if args.execute else "dry-run"
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    return 0 if stats.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
