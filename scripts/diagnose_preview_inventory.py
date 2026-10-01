"""Read-only inventory of persisted MemoryKeeper image previews.

The diagnostic compares each active MemoryKeeper image with its persisted
preview using the same media probing, storage path resolution, EXIF orientation,
and preview size limit used by the upload pipeline.  It never generates a
derivative and never writes to the database or filesystem.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, TextIO

from PIL import Image
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.common.database import SessionLocal
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.services.media_probe import (
    MediaCategory,
    MediaProbe,
    MediaProbeError,
    MediaProbeResult,
    UnsupportedMediaError,
)
from app.common.services.storage_service import StorageService


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ORIENTATION_TAG = 274
_SWAPPED_ORIENTATIONS = {5, 6, 7, 8}
_LONG_EDGE_TOLERANCE_PX = 1
_DEFAULT_PROGRESS_EVERY = 250
_DEFAULT_SAMPLE_LIMIT = 20

NORMAL = "normal_preview"
OUTDATED = "outdated_low_resolution_preview"
LARGER = "preview_larger_than_expected"
PREVIEW_MISSING = "preview_file_missing"
ORIGINAL_MISSING = "original_file_missing"
ORIGINAL_ERROR = "original_probe_or_decode_failed"
PREVIEW_ERROR = "preview_probe_or_decode_failed"
UNSUPPORTED = "unsupported_or_unclassified"

_CATEGORIES = (
    NORMAL,
    OUTDATED,
    LARGER,
    PREVIEW_MISSING,
    ORIGINAL_MISSING,
    ORIGINAL_ERROR,
    PREVIEW_ERROR,
    UNSUPPORTED,
)


@dataclass(frozen=True)
class PreviewInventoryInput:
    common_file_id: int
    file_id: str
    original_name: str
    extension: str | None
    mime_type: str | None
    original_path: str | None
    preview_path: str | None


@dataclass(frozen=True)
class PreviewInventorySample:
    file_id: str
    mime_type: str | None
    original_path: str | None
    preview_path: str | None
    original_size: tuple[int, int] | None = None
    expected_preview_long_edge: int | None = None
    actual_preview_size: tuple[int, int] | None = None
    actual_preview_long_edge: int | None = None
    difference: int | None = None
    reason: str | None = None


@dataclass
class FormatStats:
    total: int = 0
    normal: int = 0
    outdated: int = 0
    larger: int = 0
    missing: int = 0
    error: int = 0


@dataclass
class PreviewInventoryReport:
    total_active_memorykeeper_images: int = 0
    normal_preview: int = 0
    outdated_low_resolution_preview: int = 0
    preview_larger_than_expected: int = 0
    preview_file_missing: int = 0
    original_file_missing: int = 0
    original_probe_or_decode_failed: int = 0
    preview_probe_or_decode_failed: int = 0
    unsupported_or_unclassified: int = 0
    by_format: dict[str, FormatStats] = field(
        default_factory=lambda: defaultdict(FormatStats)
    )
    samples: dict[str, list[PreviewInventorySample]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def record(
        self,
        category: str,
        *,
        format_name: str,
        sample: PreviewInventorySample,
        sample_limit: int,
    ) -> None:
        if category not in _CATEGORIES:
            raise ValueError(f"unknown preview inventory category: {category}")
        setattr(self, category, getattr(self, category) + 1)

        format_stats = self.by_format[format_name]
        format_stats.total += 1
        if category == NORMAL:
            format_stats.normal += 1
        elif category == OUTDATED:
            format_stats.outdated += 1
        elif category == LARGER:
            format_stats.larger += 1
        elif category in {PREVIEW_MISSING, ORIGINAL_MISSING}:
            format_stats.missing += 1
        else:
            format_stats.error += 1

        category_samples = self.samples[category]
        if len(category_samples) < sample_limit:
            category_samples.append(sample)

    def to_dict(
        self,
        *,
        limit: int | None,
        file_id: str | None,
        elapsed_seconds: float,
    ) -> dict[str, object]:
        total = self.total_active_memorykeeper_images
        error_count = (
            self.preview_file_missing
            + self.original_file_missing
            + self.original_probe_or_decode_failed
            + self.preview_probe_or_decode_failed
            + self.unsupported_or_unclassified
        )
        regeneration_candidates = (
            self.outdated_low_resolution_preview
            + self.preview_larger_than_expected
            + self.preview_file_missing
            + self.preview_probe_or_decode_failed
        )

        summary = {
            category: getattr(self, category)
            for category in _CATEGORIES
        }
        summary.update(
            {
                "total_active_memorykeeper_images": total,
                "regeneration_candidate_count": regeneration_candidates,
                "normal_percentage": _percentage(self.normal_preview, total),
                "outdated_percentage": _percentage(
                    self.outdated_low_resolution_preview,
                    total,
                ),
                "error_percentage": _percentage(error_count, total),
                "regeneration_candidate_percentage": _percentage(
                    regeneration_candidates,
                    total,
                ),
            }
        )
        return {
            "mode": "read-only",
            "filters": {"limit": limit, "file_id": file_id},
            "preview_spec": {
                "max_size": list(StorageService.PREVIEW_MAX_SIZE),
                "long_edge_tolerance_px": _LONG_EDGE_TOLERANCE_PX,
                "expected_long_edge": "min(oriented_original_long_edge, 2560)",
            },
            "summary": summary,
            "by_format": {
                key: asdict(value)
                for key, value in sorted(self.by_format.items())
            },
            "samples": {
                key: [asdict(sample) for sample in values]
                for key, values in sorted(self.samples.items())
                if values
            },
            "elapsed_seconds": round(elapsed_seconds, 3),
        }


def diagnose_preview_inventory(
    db: Session,
    *,
    storage_service: StorageService,
    media_probe: MediaProbe | None = None,
    limit: int | None = None,
    file_id: str | None = None,
    progress_every: int = _DEFAULT_PROGRESS_EVERY,
    sample_limit: int = _DEFAULT_SAMPLE_LIMIT,
    progress_stream: TextIO | None = sys.stderr,
) -> dict[str, object]:
    """Inspect active MemoryKeeper image previews without mutating state."""
    probe = media_probe or MediaProbe()
    inputs = _load_inventory_inputs(db, limit=limit, file_id=file_id)
    report = PreviewInventoryReport(
        total_active_memorykeeper_images=len(inputs),
    )
    started = time.monotonic()

    for processed, item in enumerate(inputs, start=1):
        _inspect_one(
            report,
            item,
            storage_service=storage_service,
            media_probe=probe,
            sample_limit=sample_limit,
        )
        if progress_stream is not None and (
            processed % progress_every == 0 or processed == len(inputs)
        ):
            errors = (
                report.preview_file_missing
                + report.original_file_missing
                + report.original_probe_or_decode_failed
                + report.preview_probe_or_decode_failed
                + report.unsupported_or_unclassified
            )
            progress = {
                "processed": processed,
                "total": len(inputs),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "normal": report.normal_preview,
                "outdated": report.outdated_low_resolution_preview,
                "missing": (
                    report.preview_file_missing + report.original_file_missing
                ),
                "error": errors,
            }
            print(json.dumps(progress, sort_keys=True), file=progress_stream)

    return report.to_dict(
        limit=limit,
        file_id=file_id,
        elapsed_seconds=time.monotonic() - started,
    )


def _inspect_one(
    report: PreviewInventoryReport,
    item: PreviewInventoryInput,
    *,
    storage_service: StorageService,
    media_probe: MediaProbe,
    sample_limit: int,
) -> None:
    fallback_format = _fallback_format(item)
    base_sample = {
        "file_id": item.file_id,
        "mime_type": item.mime_type,
        "original_path": item.original_path,
        "preview_path": item.preview_path,
    }

    if not item.original_path:
        report.record(
            ORIGINAL_MISSING,
            format_name=fallback_format,
            sample=PreviewInventorySample(
                **base_sample,
                reason="missing original_path",
            ),
            sample_limit=sample_limit,
        )
        return

    try:
        original = _resolve_under_root(
            storage_service,
            item.original_path,
            expected_root=storage_service.original_root,
        )
    except (OSError, ValueError):
        report.record(
            UNSUPPORTED,
            format_name=fallback_format,
            sample=PreviewInventorySample(
                **base_sample,
                reason="unsafe original path",
            ),
            sample_limit=sample_limit,
        )
        return
    if not original.is_file():
        report.record(
            ORIGINAL_MISSING,
            format_name=fallback_format,
            sample=PreviewInventorySample(
                **base_sample,
                reason="original file is missing",
            ),
            sample_limit=sample_limit,
        )
        return

    try:
        media = media_probe.probe(original, filename=item.original_name)
    except UnsupportedMediaError as exc:
        category = (
            ORIGINAL_ERROR if _is_expected_supported_image(item) else UNSUPPORTED
        )
        report.record(
            category,
            format_name=fallback_format,
            sample=PreviewInventorySample(
                **base_sample,
                reason=_safe_reason(exc),
            ),
            sample_limit=sample_limit,
        )
        return
    except Exception as exc:
        report.record(
            ORIGINAL_ERROR,
            format_name=fallback_format,
            sample=PreviewInventorySample(
                **base_sample,
                reason=_safe_reason(exc),
            ),
            sample_limit=sample_limit,
        )
        return

    format_name = _format_name(media, fallback=fallback_format)
    if media.category not in {MediaCategory.IMAGE, MediaCategory.HEIC}:
        report.record(
            UNSUPPORTED,
            format_name=format_name,
            sample=PreviewInventorySample(
                **base_sample,
                reason=f"unexpected media category: {media.category.value}",
            ),
            sample_limit=sample_limit,
        )
        return
    if not media.width or not media.height:
        report.record(
            ORIGINAL_ERROR,
            format_name=format_name,
            sample=PreviewInventorySample(
                **base_sample,
                reason="original dimensions are unavailable",
            ),
            sample_limit=sample_limit,
        )
        return

    try:
        orientation = _read_orientation(original)
    except Exception as exc:
        report.record(
            ORIGINAL_ERROR,
            format_name=format_name,
            sample=PreviewInventorySample(
                **base_sample,
                reason=f"orientation:{_safe_reason(exc)}",
            ),
            sample_limit=sample_limit,
        )
        return

    original_size = _oriented_size(
        (int(media.width), int(media.height)),
        orientation=orientation,
    )
    expected_long_edge = min(
        max(original_size),
        max(storage_service.PREVIEW_MAX_SIZE),
    )

    if not item.preview_path:
        report.record(
            PREVIEW_MISSING,
            format_name=format_name,
            sample=PreviewInventorySample(
                **base_sample,
                original_size=original_size,
                expected_preview_long_edge=expected_long_edge,
                reason="missing preview_path",
            ),
            sample_limit=sample_limit,
        )
        return

    try:
        preview = _resolve_under_root(
            storage_service,
            item.preview_path,
            expected_root=storage_service.preview_root,
        )
    except (OSError, ValueError):
        report.record(
            PREVIEW_ERROR,
            format_name=format_name,
            sample=PreviewInventorySample(
                **base_sample,
                original_size=original_size,
                expected_preview_long_edge=expected_long_edge,
                reason="unsafe preview path",
            ),
            sample_limit=sample_limit,
        )
        return
    if not preview.is_file():
        report.record(
            PREVIEW_MISSING,
            format_name=format_name,
            sample=PreviewInventorySample(
                **base_sample,
                original_size=original_size,
                expected_preview_long_edge=expected_long_edge,
                reason="preview file is missing",
            ),
            sample_limit=sample_limit,
        )
        return

    try:
        actual_size = _read_verified_size(preview)
    except Exception as exc:
        report.record(
            PREVIEW_ERROR,
            format_name=format_name,
            sample=PreviewInventorySample(
                **base_sample,
                original_size=original_size,
                expected_preview_long_edge=expected_long_edge,
                reason=_safe_reason(exc),
            ),
            sample_limit=sample_limit,
        )
        return

    actual_long_edge = max(actual_size)
    difference = actual_long_edge - expected_long_edge
    sample = PreviewInventorySample(
        **base_sample,
        original_size=original_size,
        expected_preview_long_edge=expected_long_edge,
        actual_preview_size=actual_size,
        actual_preview_long_edge=actual_long_edge,
        difference=difference,
    )
    if difference < -_LONG_EDGE_TOLERANCE_PX:
        category = OUTDATED
    elif difference > _LONG_EDGE_TOLERANCE_PX:
        category = LARGER
    else:
        category = NORMAL
    report.record(
        category,
        format_name=format_name,
        sample=sample,
        sample_limit=sample_limit,
    )


def _load_inventory_inputs(
    db: Session,
    *,
    limit: int | None,
    file_id: str | None,
) -> list[PreviewInventoryInput]:
    """Take a small scalar snapshot, then release the DB transaction."""
    db.rollback()
    try:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        with db.no_autoflush:
            query = (
                db.query(
                    CommonFile.id,
                    CommonFile.file_id,
                    CommonFile.original_name,
                    CommonFile.extension,
                    CommonFile.mime_type,
                    CommonFile.original_path,
                    CommonFile.preview_path,
                )
                .join(
                    CommonFileService,
                    CommonFileService.file_id == CommonFile.id,
                )
                .filter(CommonFileService.service_name == "MemoryKeeper")
                .filter(CommonFile.deleted.is_(False))
                .filter(func.lower(CommonFile.mime_type).like("image/%"))
                .order_by(CommonFile.id.asc())
            )
            if file_id:
                query = query.filter(CommonFile.file_id == file_id)
            if limit is not None:
                query = query.limit(limit)
            rows = query.all()
            return [
                PreviewInventoryInput(
                    common_file_id=int(row.id),
                    file_id=str(row.file_id),
                    original_name=str(row.original_name),
                    extension=row.extension,
                    mime_type=row.mime_type,
                    original_path=row.original_path,
                    preview_path=row.preview_path,
                )
                for row in rows
            ]
    finally:
        db.rollback()


def _resolve_under_root(
    storage_service: StorageService,
    stored_path: str,
    *,
    expected_root: Path,
) -> Path:
    storage_root = storage_service.storage_root.resolve(strict=False)
    root = expected_root.resolve(strict=False)
    root.relative_to(storage_root)
    resolved = storage_service.resolve_storage_path(stored_path).resolve(strict=False)
    resolved.relative_to(root)
    return resolved


def _read_orientation(path: Path) -> int:
    """Read only orientation metadata; do not transpose/decode the pixel buffer."""
    with Image.open(path) as image:
        value = image.getexif().get(_ORIENTATION_TAG, 1)
    try:
        orientation = int(value)
    except (TypeError, ValueError):
        return 1
    return orientation if 1 <= orientation <= 8 else 1


def _oriented_size(
    size: tuple[int, int],
    *,
    orientation: int,
) -> tuple[int, int]:
    if orientation in _SWAPPED_ORIENTATIONS:
        return size[1], size[0]
    return size


def _read_verified_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        size = (int(image.width), int(image.height))
        image.verify()
    if size[0] < 1 or size[1] < 1:
        raise ValueError("image dimensions must be positive")
    return size


def _format_name(media: MediaProbeResult, *, fallback: str) -> str:
    if media.category == MediaCategory.HEIC:
        return "HEIC/HEIF"
    normalized = str(media.format_name or "").strip().upper()
    return normalized or fallback


def _fallback_format(item: PreviewInventoryInput) -> str:
    mime_type = str(item.mime_type or "").strip().casefold()
    extension = str(item.extension or Path(item.original_name).suffix).strip().casefold()
    if mime_type in {"image/heic", "image/heif"} or extension in {".heic", ".heif"}:
        return "HEIC/HEIF"
    mapping = {
        "image/jpeg": "JPEG",
        "image/png": "PNG",
        "image/webp": "WEBP",
        "image/gif": "GIF",
        "image/tiff": "TIFF",
        "image/bmp": "BMP",
    }
    return mapping.get(mime_type, "OTHER")


def _is_expected_supported_image(item: PreviewInventoryInput) -> bool:
    mime_type = str(item.mime_type or "").strip().casefold()
    extension = str(item.extension or Path(item.original_name).suffix).strip().casefold()
    return mime_type in {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/tiff",
        "image/bmp",
        "image/heic",
        "image/heif",
    } or extension in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".tif",
        ".tiff",
        ".bmp",
        ".heic",
        ".heif",
    }


def _safe_reason(exc: Exception) -> str:
    if isinstance(exc, MediaProbeError):
        reason = " ".join(str(exc).split())
        return reason[:240] if reason else type(exc).__name__
    return type(exc).__name__


def _percentage(value: int, total: int) -> float:
    if total == 0:
        return 0.0
    return round((value / total) * 100.0, 3)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only MemoryKeeper preview size inventory",
    )
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to inspect")
    parser.add_argument(
        "--file-id",
        default=None,
        help="Exact common_files.file_id lowercase SHA-256",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=_DEFAULT_PROGRESS_EVERY,
        help="Write one progress line to stderr every N files",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=_DEFAULT_SAMPLE_LIMIT,
        help="Maximum reported samples per result category",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    if args.file_id and not _SHA256_RE.fullmatch(args.file_id):
        raise SystemExit("--file-id must be a lowercase SHA-256 digest")
    if args.progress_every < 1:
        raise SystemExit("--progress-every must be at least 1")
    if args.sample_limit < 0:
        raise SystemExit("--sample-limit must be zero or greater")

    db = SessionLocal()
    try:
        result = diagnose_preview_inventory(
            db,
            storage_service=StorageService(),
            limit=args.limit,
            file_id=args.file_id,
            progress_every=args.progress_every,
            sample_limit=args.sample_limit,
        )
    except Exception as exc:
        db.rollback()
        print(
            json.dumps(
                {
                    "error": "preview inventory failed",
                    "exception_class": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        db.close()

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
