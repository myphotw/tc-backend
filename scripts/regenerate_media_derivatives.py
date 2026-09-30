"""Regenerate persisted derivatives from authoritative CommonFile originals.

Dry-run is the default. Execute mode replaces each derivative atomically through
``MediaDerivativeService`` and only updates DB paths when the canonical path has
changed. Originals, service links, metadata, Vision jobs, and upload jobs are
never modified.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.common.database import SessionLocal
from app.common.models.file import CommonFile
from app.common.services.media_derivatives import MediaDerivativeService
from app.common.services.media_probe import MediaCategory, MediaProbe, MediaProbeError
from app.common.services.storage_service import StorageService


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_REPORTED_FAILURES = 100


@dataclass
class RegenerationStats:
    scanned: int = 0
    target_files: int = 0
    image_targets: int = 0
    video_targets: int = 0
    preview_regeneration_targets: int = 0
    thumbnail_regeneration_targets: int = 0
    regenerated_files: int = 0
    db_path_updates: int = 0
    skipped: int = 0
    skipped_unsupported: int = 0
    skipped_unsafe_path: int = 0
    missing_originals: int = 0
    potential_errors: int = 0
    failed_files: int = 0
    failures: list[str] = field(default_factory=list)


def regenerate_media_derivatives(
    db: Session,
    *,
    storage_service: StorageService,
    media_probe: MediaProbe | None = None,
    derivative_service: MediaDerivativeService | None = None,
    execute: bool = False,
    limit: int | None = None,
    file_id: str | None = None,
) -> RegenerationStats:
    """Scan active common media and optionally regenerate bounded derivatives."""
    probe = media_probe or MediaProbe()
    derivatives = derivative_service or MediaDerivativeService(storage_service)
    stats = RegenerationStats()
    query = (
        db.query(
            CommonFile.id,
            CommonFile.file_id,
            CommonFile.original_name,
            CommonFile.original_path,
            CommonFile.preview_path,
            CommonFile.thumb_path,
        )
        .filter(CommonFile.deleted.is_(False))
        .order_by(CommonFile.id.asc())
    )
    if file_id:
        query = query.filter(CommonFile.file_id == file_id)
    if limit is not None:
        query = query.limit(limit)

    common_files = query.all()
    db.rollback()

    for common_file in common_files:
        stats.scanned += 1
        if not common_file.original_path:
            stats.skipped += 1
            stats.missing_originals += 1
            stats.potential_errors += 1
            _record_failure(stats, common_file.file_id, "missing-original-path")
            continue
        try:
            source = _resolve_original(storage_service, common_file.original_path)
        except (OSError, ValueError):
            stats.skipped += 1
            stats.skipped_unsafe_path += 1
            stats.potential_errors += 1
            _record_failure(stats, common_file.file_id, "unsafe-original-path")
            continue
        if not source.is_file():
            stats.skipped += 1
            stats.missing_originals += 1
            stats.potential_errors += 1
            _record_failure(stats, common_file.file_id, "missing-original")
            continue

        try:
            media = probe.probe(source, filename=common_file.original_name)
        except MediaProbeError as exc:
            stats.skipped += 1
            stats.skipped_unsupported += 1
            stats.potential_errors += 1
            _record_failure(
                stats,
                common_file.file_id,
                f"probe:{type(exc).__name__}",
            )
            continue

        create_preview = media.category in {MediaCategory.IMAGE, MediaCategory.HEIC}
        create_thumbnail = create_preview or media.category == MediaCategory.VIDEO
        if not create_thumbnail:
            stats.skipped += 1
            stats.skipped_unsupported += 1
            continue

        stats.target_files += 1
        if media.category == MediaCategory.VIDEO:
            stats.video_targets += 1
        else:
            stats.image_targets += 1
        if create_preview:
            stats.preview_regeneration_targets += 1
        stats.thumbnail_regeneration_targets += 1

        if not execute:
            continue

        try:
            result = derivatives.generate(
                original_path=source,
                file_id=common_file.file_id,
                media=media,
                create_preview=create_preview,
                create_thumbnail=create_thumbnail,
            )
        except Exception as exc:
            db.rollback()
            stats.failed_files += 1
            _record_failure(
                stats,
                common_file.file_id,
                f"generate:{type(exc).__name__}",
            )
            continue

        generated_all = True
        preview_relative: str | None = None
        thumb_relative: str | None = None
        if create_preview:
            preview_path = result.preview_path
            if preview_path is None or not preview_path.is_file():
                generated_all = False
            else:
                preview_relative = storage_service.to_relative_path(preview_path)

        thumb_path = result.thumb_path
        if thumb_path is None or not thumb_path.is_file():
            generated_all = False
        else:
            thumb_relative = storage_service.to_relative_path(thumb_path)

        paths_changed = (
            preview_relative is not None
            and common_file.preview_path != preview_relative
        ) or (
            thumb_relative is not None
            and common_file.thumb_path != thumb_relative
        )
        if paths_changed:
            try:
                persisted = (
                    db.query(CommonFile)
                    .filter(CommonFile.id == common_file.id)
                    .filter(CommonFile.deleted.is_(False))
                    .one_or_none()
                )
                if persisted is None:
                    raise RuntimeError("common file became inactive")
                if preview_relative is not None:
                    persisted.preview_path = preview_relative
                if thumb_relative is not None:
                    persisted.thumb_path = thumb_relative
                db.commit()
            except Exception as exc:
                db.rollback()
                stats.failed_files += 1
                _record_failure(
                    stats,
                    common_file.file_id,
                    f"database:{type(exc).__name__}",
                )
                continue
            stats.db_path_updates += 1

        if generated_all and not result.failures:
            stats.regenerated_files += 1
        else:
            stats.failed_files += 1
            reasons = ",".join(result.failures) or "missing-generated-derivative"
            _record_failure(stats, common_file.file_id, reasons)

    return stats


def _resolve_original(storage_service: StorageService, stored_path: str) -> Path:
    original_root = storage_service.original_root.resolve(strict=False)
    resolved = storage_service.resolve_storage_path(stored_path).resolve(strict=False)
    try:
        resolved.relative_to(original_root)
    except ValueError as exc:
        raise ValueError("original path is outside configured storage") from exc
    return resolved


def _record_failure(stats: RegenerationStats, file_id: str, reason: str) -> None:
    if len(stats.failures) < _MAX_REPORTED_FAILURES:
        stats.failures.append(f"{file_id}:{reason}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Regenerate active CommonFile preview/thumbnail assets",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and report only (default)",
    )
    mode.add_argument("--execute", action="store_true", help="Replace derivative files")
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to scan")
    parser.add_argument(
        "--file-id",
        default=None,
        help="Exact common_files.file_id SHA-256",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    if args.file_id and not _SHA256_RE.fullmatch(args.file_id):
        raise SystemExit("--file-id must be a lowercase SHA-256 digest")

    db = SessionLocal()
    try:
        stats = regenerate_media_derivatives(
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
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if stats.potential_errors == 0 and stats.failed_files == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
