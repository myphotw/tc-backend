"""Backfill only missing EXIF-derived metadata from existing original assets.

The default mode is dry-run. This script never moves, copies, deletes, hashes,
or regenerates media files and never creates Vision jobs.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.common.database import SessionLocal
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.repositories.history_repository import HistoryRepository
from app.common.repositories.metadata_priority import MetadataPriority
from app.common.services.photo_analysis import ExifReader
from app.common.services.storage_service import StorageService


EXIF_DERIVED_FIELDS = (
    "camera_make",
    "camera_model",
    "lens",
    "datetime_original",
    "gps_lat",
    "gps_lon",
    "gps_alt",
    "iso",
    "f_number",
    "exposure_time",
    "focal_length",
    "orientation",
    "image_width",
    "image_height",
)


@dataclass
class BackfillStats:
    scanned: int = 0
    eligible: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0


def backfill_exif_metadata(
    db: Session,
    *,
    storage_service: StorageService,
    exif_reader: ExifReader | None = None,
    execute: bool = False,
    limit: int | None = None,
) -> BackfillStats:
    """Read candidate originals and fill only currently-null EXIF fields."""
    reader = exif_reader or ExifReader()
    stats = BackfillStats()
    query = (
        db.query(CommonFile, CommonFileMetadata)
        .outerjoin(CommonFileMetadata, CommonFileMetadata.file_id == CommonFile.id)
        .filter(CommonFile.deleted.is_(False))
        .filter(CommonFile.original_path.isnot(None))
        .filter(CommonFile.original_path != "")
        .filter(CommonFileMetadata.datetime_original.is_(None))
        .order_by(CommonFile.id.asc())
    )
    if limit is not None:
        query = query.limit(limit)

    for common_file, metadata in query.all():
        stats.scanned += 1
        stats.eligible += 1
        try:
            original_path = _resolve_original_path(
                storage_service,
                common_file.original_path,
            )
            if not original_path.is_file():
                stats.failed += 1
                continue

            extracted = reader.read(original_path)
            missing_values = _missing_exif_values(metadata, extracted)
            if not missing_values:
                stats.unchanged += 1
                continue

            if execute:
                changed = _apply_missing_values(
                    db,
                    common_file_id=common_file.id,
                    values=missing_values,
                )
                if not changed:
                    stats.unchanged += 1
                    continue
            stats.updated += 1
        except Exception:
            if execute:
                db.rollback()
            stats.failed += 1

    return stats


def _resolve_original_path(
    storage_service: StorageService,
    stored_path: str,
) -> Path:
    """Resolve a DB path while refusing reads outside the original root."""
    original_root = storage_service.original_root.resolve(strict=False)
    resolved = storage_service.resolve_storage_path(stored_path).resolve(strict=False)
    try:
        resolved.relative_to(original_root)
    except ValueError as exc:
        raise ValueError("original path is outside configured storage") from exc
    return resolved


def _missing_exif_values(
    metadata: CommonFileMetadata | None,
    extracted: dict[str, Any],
) -> dict[str, Any]:
    """Select only EXIF-derived values whose current DB column is null."""
    values: dict[str, Any] = {}
    for field_name in EXIF_DERIVED_FIELDS:
        new_value = extracted.get(field_name)
        if new_value is None or new_value == "":
            continue
        current_value = getattr(metadata, field_name, None) if metadata else None
        if current_value is None:
            values[field_name] = new_value
    return values


def _apply_missing_values(
    db: Session,
    *,
    common_file_id: int,
    values: dict[str, Any],
) -> bool:
    """Persist one file's null-only patch and its metadata history atomically."""
    item = (
        db.query(CommonFileMetadata)
        .filter(CommonFileMetadata.file_id == common_file_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if item is None:
        item = CommonFileMetadata(file_id=common_file_id)
        db.add(item)
        db.flush()

    history_items: list[dict[str, Any]] = []
    for field_name, new_value in values.items():
        if getattr(item, field_name) is not None:
            continue
        setattr(item, field_name, new_value)
        history_items.append(
            {
                "file_id": common_file_id,
                "field_name": field_name,
                "old_value": None,
                "new_value": new_value,
                "source": "EXIF",
                "priority": MetadataPriority.EXIF,
                "modified_by": "backfill_exif_metadata.py",
                "approved": False,
            }
        )

    if not history_items:
        db.rollback()
        return False

    HistoryRepository(db).create_histories(items=history_items, commit=False)
    db.commit()
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill null EXIF metadata from existing original assets",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and report only (default)",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Persist null-only metadata updates",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum candidate rows to scan",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")

    db = SessionLocal()
    try:
        stats = backfill_exif_metadata(
            db,
            storage_service=StorageService(),
            execute=bool(args.execute),
            limit=args.limit,
        )
    finally:
        db.close()

    summary = asdict(stats)
    summary["mode"] = "execute" if args.execute else "dry-run"
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    return 0 if stats.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
