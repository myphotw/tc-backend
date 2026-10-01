"""Safely replace only outdated MemoryKeeper image previews.

Dry-run is the default. Execute mode writes a verified staging image beside the
existing preview and atomically replaces the final path only after validation.
Originals, thumbnails, database rows, and already-current previews are never
modified.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, TextIO
from uuid import uuid4

from PIL import Image, ImageOps
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
    register_heif_opener,
)
from app.common.services.storage_service import StorageService


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ORIENTATION_TAG = 274
_SWAPPED_ORIENTATIONS = {5, 6, 7, 8}
_LONG_EDGE_TOLERANCE_PX = 1
_DEFAULT_PROGRESS_EVERY = 100
_MAX_FAILURE_SAMPLES = 20
_EXPECTED_FORMATS_BY_SUFFIX = {
    ".jpg": {"JPEG"},
    ".jpeg": {"JPEG"},
    ".png": {"PNG"},
    ".webp": {"WEBP"},
    ".gif": {"GIF"},
    ".bmp": {"BMP"},
    ".tif": {"TIFF"},
    ".tiff": {"TIFF"},
}


@dataclass(frozen=True)
class PreviewRow:
    common_file_id: int
    file_id: str
    original_name: str
    original_path: str | None
    preview_path: str | None


@dataclass(frozen=True)
class OutdatedPreview:
    row: PreviewRow
    original: Path
    preview: Path
    media: MediaProbeResult
    expected_long_edge: int
    actual_long_edge: int


@dataclass
class RegenerationStats:
    selected_rows: int = 0
    scanned: int = 0
    candidates: int = 0
    regenerated: int = 0
    skipped_normal: int = 0
    skipped_larger: int = 0
    missing: int = 0
    probe_decode_error: int = 0
    preview_decode_error: int = 0
    unsupported: int = 0
    failed: int = 0
    interrupted: bool = False
    failure_samples: list[dict[str, object]] = field(default_factory=list)

    def record_failure(
        self,
        row: PreviewRow,
        *,
        stage: str,
        reason: str,
    ) -> None:
        if len(self.failure_samples) >= _MAX_FAILURE_SAMPLES:
            return
        self.failure_samples.append(
            {
                "file_id": row.file_id,
                "stage": stage,
                "reason": reason[:240],
            }
        )


def regenerate_outdated_previews(
    db: Session,
    *,
    storage_service: StorageService,
    media_probe: MediaProbe | None = None,
    execute: bool = False,
    limit: int | None = None,
    file_id: str | None = None,
    progress_every: int = _DEFAULT_PROGRESS_EVERY,
    progress_stream: TextIO | None = sys.stdout,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Inspect previews and optionally atomically replace outdated ones."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")
    if progress_every < 1:
        raise ValueError("progress_every must be at least 1")

    probe = media_probe or MediaProbe()
    rows = _load_rows(db, file_id=file_id)
    stats = RegenerationStats(selected_rows=len(rows))
    started = time.monotonic()

    for row in rows:
        if stop_requested is not None and stop_requested():
            stats.interrupted = True
            break
        stats.scanned += 1
        candidate = _assess_preview(
            row,
            stats=stats,
            storage_service=storage_service,
            media_probe=probe,
        )
        if candidate is not None:
            stats.candidates += 1
            if execute:
                try:
                    _replace_preview_atomically(
                        candidate,
                        storage_service=storage_service,
                    )
                except Exception as exc:
                    stats.failed += 1
                    stats.record_failure(
                        row,
                        stage="regenerate",
                        reason=_safe_reason(exc),
                    )
                else:
                    stats.regenerated += 1

        reached_limit = limit is not None and stats.candidates >= limit
        interrupted = stop_requested is not None and stop_requested()
        if interrupted:
            stats.interrupted = True
        if progress_stream is not None and (
            stats.scanned % progress_every == 0
            or reached_limit
            or interrupted
            or stats.scanned == len(rows)
        ):
            _print_progress(
                stats,
                elapsed_seconds=time.monotonic() - started,
                stream=progress_stream,
            )
        if reached_limit or interrupted:
            break

    result = asdict(stats)
    result.update(
        {
            "mode": "execute" if execute else "dry-run",
            "candidate_limit": limit,
            "file_id": file_id,
            "outdated": stats.candidates,
            "regeneration_candidate": stats.candidates,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    )
    return result


def _load_rows(db: Session, *, file_id: str | None) -> list[PreviewRow]:
    """Load a scalar snapshot and release the database transaction."""
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
            rows = query.all()
            return [
                PreviewRow(
                    common_file_id=int(row.id),
                    file_id=str(row.file_id),
                    original_name=str(row.original_name),
                    original_path=row.original_path,
                    preview_path=row.preview_path,
                )
                for row in rows
            ]
    finally:
        db.rollback()


def _assess_preview(
    row: PreviewRow,
    *,
    stats: RegenerationStats,
    storage_service: StorageService,
    media_probe: MediaProbe,
) -> OutdatedPreview | None:
    if not row.original_path or not row.preview_path:
        stats.missing += 1
        stats.record_failure(row, stage="path", reason="missing persisted path")
        return None

    try:
        original = _resolve_under_root(
            storage_service,
            row.original_path,
            expected_root=storage_service.original_root,
        )
        preview = _resolve_under_root(
            storage_service,
            row.preview_path,
            expected_root=storage_service.preview_root,
        )
    except (OSError, ValueError) as exc:
        stats.missing += 1
        stats.record_failure(row, stage="path", reason=_safe_reason(exc))
        return None

    if not original.is_file() or not preview.is_file():
        stats.missing += 1
        missing = "original" if not original.is_file() else "preview"
        stats.record_failure(row, stage="path", reason=f"{missing} file is missing")
        return None

    try:
        media = media_probe.probe(original, filename=row.original_name)
    except UnsupportedMediaError as exc:
        stats.unsupported += 1
        stats.record_failure(row, stage="probe", reason=_safe_reason(exc))
        return None
    except Exception as exc:
        stats.probe_decode_error += 1
        stats.record_failure(row, stage="probe", reason=_safe_reason(exc))
        return None

    if media.category not in {MediaCategory.IMAGE, MediaCategory.HEIC}:
        stats.unsupported += 1
        stats.record_failure(
            row,
            stage="probe",
            reason=f"unexpected media category: {media.category.value}",
        )
        return None
    if not media.width or not media.height:
        stats.probe_decode_error += 1
        stats.record_failure(
            row,
            stage="probe",
            reason="original dimensions are unavailable",
        )
        return None

    try:
        orientation = _read_orientation(original)
    except Exception as exc:
        stats.probe_decode_error += 1
        stats.record_failure(row, stage="orientation", reason=_safe_reason(exc))
        return None

    original_size = _oriented_size(
        (int(media.width), int(media.height)),
        orientation=orientation,
    )
    expected_long_edge = min(
        max(original_size),
        max(storage_service.PREVIEW_MAX_SIZE),
    )

    try:
        actual_size, _actual_format = _read_verified_image(preview)
    except Exception as exc:
        stats.preview_decode_error += 1
        stats.record_failure(row, stage="preview", reason=_safe_reason(exc))
        return None

    actual_long_edge = max(actual_size)
    difference = actual_long_edge - expected_long_edge
    if difference < -_LONG_EDGE_TOLERANCE_PX:
        return OutdatedPreview(
            row=row,
            original=original,
            preview=preview,
            media=media,
            expected_long_edge=expected_long_edge,
            actual_long_edge=actual_long_edge,
        )
    if difference > _LONG_EDGE_TOLERANCE_PX:
        stats.skipped_larger += 1
    else:
        stats.skipped_normal += 1
    return None


def _replace_preview_atomically(
    candidate: OutdatedPreview,
    *,
    storage_service: StorageService,
) -> None:
    final = candidate.preview
    suffix = final.suffix.casefold()
    if suffix not in _EXPECTED_FORMATS_BY_SUFFIX:
        raise ValueError(f"unsupported preview extension: {suffix or '<none>'}")
    if not final.is_file():
        raise FileNotFoundError("existing preview disappeared before replacement")

    staging = final.parent / (
        f".{final.stem}.preview-regeneration.{uuid4().hex}{final.suffix}"
    )
    try:
        if candidate.media.category == MediaCategory.HEIC:
            register_heif_opener()
        with Image.open(candidate.original) as source:
            oriented = ImageOps.exif_transpose(source)
            try:
                resized = oriented.copy()
            finally:
                oriented.close()
        try:
            resized.thumbnail(
                storage_service.PREVIEW_MAX_SIZE,
                Image.Resampling.LANCZOS,
            )
            storage_service._save_image(resized, staging, suffix)
        finally:
            resized.close()

        _validate_staged_preview(
            staging,
            expected_long_edge=candidate.expected_long_edge,
            expected_formats=_EXPECTED_FORMATS_BY_SUFFIX[suffix],
        )
        _replace_staged_preview(staging, final)
    finally:
        staging.unlink(missing_ok=True)


def _replace_staged_preview(staging: Path, final: Path) -> None:
    os.replace(staging, final)


def _validate_staged_preview(
    path: Path,
    *,
    expected_long_edge: int,
    expected_formats: set[str],
) -> None:
    if not path.is_file() or path.stat().st_size < 1:
        raise ValueError("staged preview is missing or empty")
    size, image_format = _read_verified_image(path)
    if image_format not in expected_formats:
        raise ValueError(f"unexpected staged preview format: {image_format}")
    if abs(max(size) - expected_long_edge) > _LONG_EDGE_TOLERANCE_PX:
        raise ValueError(
            "staged preview long edge does not match the expected size"
        )


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


def _read_verified_image(path: Path) -> tuple[tuple[int, int], str]:
    with Image.open(path) as image:
        size = (int(image.width), int(image.height))
        image_format = str(image.format or "").upper()
        image.verify()
    if size[0] < 1 or size[1] < 1:
        raise ValueError("image dimensions must be positive")
    if not image_format:
        raise ValueError("image format is unavailable")
    return size, image_format


def _safe_reason(exc: Exception) -> str:
    if isinstance(exc, MediaProbeError):
        reason = " ".join(str(exc).split())
        return reason[:240] if reason else type(exc).__name__
    reason = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {reason}"[:240]


def _print_progress(
    stats: RegenerationStats,
    *,
    elapsed_seconds: float,
    stream: TextIO,
) -> None:
    print(
        json.dumps(
            {
                "event": "progress",
                "scanned": stats.scanned,
                "selected_rows": stats.selected_rows,
                "candidates": stats.candidates,
                "regenerated": stats.regenerated,
                "skipped_normal": stats.skipped_normal,
                "failed": stats.failed,
                "missing": stats.missing,
                "elapsed_seconds": round(elapsed_seconds, 3),
            },
            sort_keys=True,
        ),
        file=stream,
        flush=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely regenerate only outdated MemoryKeeper previews",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect and report only (default)",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Atomically replace verified outdated previews",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum outdated candidates to inspect or replace",
    )
    parser.add_argument(
        "--file-id",
        default=None,
        help="Exact common_files.file_id lowercase SHA-256",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=_DEFAULT_PROGRESS_EVERY,
        help="Write progress to stdout every N scanned rows",
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

    stop_state = {"requested": False}

    def request_stop(_signum, _frame) -> None:
        stop_state["requested"] = True
        print(
            json.dumps({"event": "stop_requested"}, sort_keys=True),
            flush=True,
        )

    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    db = SessionLocal()
    try:
        result = regenerate_outdated_previews(
            db,
            storage_service=StorageService(),
            execute=bool(args.execute),
            limit=args.limit,
            file_id=args.file_id,
            progress_every=args.progress_every,
            stop_requested=lambda: stop_state["requested"],
        )
    except Exception as exc:
        db.rollback()
        print(
            json.dumps(
                {
                    "error": "preview regeneration failed",
                    "exception_class": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2
    finally:
        db.close()
        signal.signal(signal.SIGTERM, previous_sigterm)

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    error_count = (
        int(result["failed"])
        + int(result["missing"])
        + int(result["probe_decode_error"])
        + int(result["preview_decode_error"])
        + int(result["unsupported"])
    )
    if result["interrupted"]:
        return 130
    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
