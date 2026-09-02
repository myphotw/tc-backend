"""Backfill persisted derivatives for MemoryKeeper HEIC and video assets.

Dry-run is the default. This script never reuploads originals, creates Vision
jobs, or changes service links/metadata.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import asdict, dataclass

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.common.database import SessionLocal
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.vision_job import CommonVisionJob
from app.common.services.media_derivatives import MediaDerivativeService
from app.common.services.media_probe import (
    MediaCategory,
    MediaProbe,
    MediaProbeError,
)
from app.common.services.storage_service import StorageService


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class BackfillStats:
    scanned: int = 0
    eligible: int = 0
    would_update: int = 0
    updated: int = 0
    skipped_complete: int = 0
    skipped_unsupported: int = 0
    skipped_unsafe_path: int = 0
    failed: int = 0
    shared_files: int = 0
    existing_video_vision_jobs: int = 0


def backfill_media_derivatives(
    db: Session,
    *,
    storage_service: StorageService,
    media_probe: MediaProbe | None = None,
    derivative_service: MediaDerivativeService | None = None,
    execute: bool = False,
    limit: int | None = None,
    file_id: str | None = None,
) -> BackfillStats:
    """Probe and optionally generate missing derivatives, one file at a time."""
    probe = media_probe or MediaProbe()
    derivatives = derivative_service or MediaDerivativeService(storage_service)
    stats = BackfillStats()
    service_counts = (
        db.query(
            CommonFileService.file_id.label("common_file_id"),
            func.count(CommonFileService.id).label("service_count"),
        )
        .group_by(CommonFileService.file_id)
        .subquery()
    )
    vision_counts = (
        db.query(
            CommonVisionJob.file_id.label("common_file_id"),
            func.count(CommonVisionJob.id).label("vision_count"),
        )
        .group_by(CommonVisionJob.file_id)
        .subquery()
    )
    query = (
        db.query(
            CommonFile,
            service_counts.c.service_count,
            vision_counts.c.vision_count,
        )
        .join(
            CommonFileService,
            CommonFileService.file_id == CommonFile.id,
        )
        .outerjoin(service_counts, service_counts.c.common_file_id == CommonFile.id)
        .outerjoin(vision_counts, vision_counts.c.common_file_id == CommonFile.id)
        .filter(CommonFileService.service_name == "MemoryKeeper")
        .filter(CommonFile.deleted.is_(False))
        .filter(CommonFile.original_path.isnot(None))
        .filter(CommonFile.original_path != "")
        .filter(
            or_(
                CommonFile.preview_path.is_(None),
                CommonFile.preview_path == "",
                CommonFile.thumb_path.is_(None),
                CommonFile.thumb_path == "",
            )
        )
        .order_by(CommonFile.id.asc())
    )
    if file_id:
        query = query.filter(CommonFile.file_id == file_id)
    if limit is not None:
        query = query.limit(limit)

    for common_file, service_count, vision_count in query.all():
        stats.scanned += 1
        try:
            source = _resolve_original(storage_service, common_file.original_path)
        except (OSError, ValueError):
            stats.skipped_unsafe_path += 1
            continue
        if not source.is_file():
            stats.failed += 1
            continue
        try:
            media = probe.probe(source, filename=common_file.original_name)
        except MediaProbeError:
            stats.skipped_unsupported += 1
            continue
        if media.category not in {MediaCategory.HEIC, MediaCategory.VIDEO}:
            stats.skipped_unsupported += 1
            continue

        needs_preview = media.category == MediaCategory.HEIC and not _asset_exists(
            storage_service,
            common_file.preview_path,
            expected_root=storage_service.preview_root,
        )
        needs_thumb = not _asset_exists(
            storage_service,
            common_file.thumb_path,
            expected_root=storage_service.thumb_root,
        )
        if not needs_preview and not needs_thumb:
            stats.skipped_complete += 1
            continue

        stats.eligible += 1
        stats.would_update += 1
        if int(service_count or 0) > 1:
            stats.shared_files += 1
        if media.category == MediaCategory.VIDEO:
            stats.existing_video_vision_jobs += int(vision_count or 0)
        if not execute:
            continue

        try:
            result = derivatives.generate(
                original_path=source,
                file_id=common_file.file_id,
                media=media,
                create_preview=needs_preview,
                create_thumbnail=needs_thumb,
            )
        except Exception:
            db.rollback()
            stats.failed += 1
            continue
        derivative_written = False
        if (
            needs_preview
            and result.preview_path is not None
            and result.preview_path.is_file()
        ):
            common_file.preview_path = storage_service.to_relative_path(
                result.preview_path
            )
            derivative_written = True
        if needs_thumb and result.thumb_path is not None and result.thumb_path.is_file():
            common_file.thumb_path = storage_service.to_relative_path(result.thumb_path)
            derivative_written = True
        if not derivative_written:
            db.rollback()
            stats.failed += 1
            continue
        if common_file.width is None and result.width is not None:
            common_file.width = result.width
        if common_file.height is None and result.height is not None:
            common_file.height = result.height
        try:
            db.commit()
        except Exception:
            db.rollback()
            stats.failed += 1
            continue
        stats.updated += 1

    return stats


def _resolve_original(storage_service: StorageService, stored_path: str):
    original_root = storage_service.original_root.resolve(strict=False)
    resolved = storage_service.resolve_storage_path(stored_path).resolve(strict=False)
    try:
        resolved.relative_to(original_root)
    except ValueError as exc:
        raise ValueError("original path is outside configured storage") from exc
    return resolved


def _asset_exists(
    storage_service: StorageService,
    stored_path: str | None,
    *,
    expected_root,
) -> bool:
    if not stored_path:
        return False
    try:
        root = expected_root.resolve(strict=False)
        resolved = storage_service.resolve_storage_path(stored_path).resolve(strict=False)
        resolved.relative_to(root)
        return resolved.is_file()
    except (OSError, ValueError):
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill MemoryKeeper HEIC/video derivatives",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read and report only (default)")
    mode.add_argument("--execute", action="store_true", help="Generate files and persist paths")
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to scan")
    parser.add_argument("--file-id", default=None, help="Exact common_files.file_id SHA-256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    if args.file_id and not _SHA256_RE.fullmatch(args.file_id):
        raise SystemExit("--file-id must be a lowercase SHA-256 digest")
    db = SessionLocal()
    try:
        stats = backfill_media_derivatives(
            db,
            storage_service=StorageService(),
            execute=bool(args.execute),
            limit=args.limit,
            file_id=args.file_id,
        )
    finally:
        db.close()
    summary = asdict(stats)
    summary["mode"] = "execute" if args.execute else "dry-run"
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    return 0 if stats.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
