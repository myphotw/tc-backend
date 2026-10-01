"""Read-only diagnostics for active CommonFile media probe failures.

The script intentionally performs no derivative generation and no database or
filesystem writes.  PostgreSQL scans run in an explicitly read-only
transaction, which is closed before any potentially long-running media probe.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from PIL import Image
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.common.database import SessionLocal
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.services.media_probe import (
    MediaCommandTimeout,
    MediaProbe,
    MediaProbeError,
    MediaProbeResult,
    MediaToolUnavailableError,
    UnsupportedMediaError,
)
from app.common.services.storage_service import StorageService


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SAMPLE_FILE_IDS = 3
_UNLINKED_SERVICE = "unlinked"
_MISSING_VALUE = "<none>"


@dataclass(frozen=True)
class ProbeInput:
    common_file_id: int
    file_id: str
    original_name: str
    original_path: str | None
    extension: str
    mime_type: str
    filename_suffix: str
    service_names: tuple[str, ...]


@dataclass
class ProbeDiagnosticReport:
    scanned: int = 0
    probe_success: int = 0
    probe_failed: int = 0
    success_by_category: Counter[str] = field(default_factory=Counter)
    failure_by_exception: Counter[str] = field(default_factory=Counter)
    failure_by_reason: Counter[str] = field(default_factory=Counter)
    failure_by_diagnostic_category: Counter[str] = field(default_factory=Counter)
    failure_by_service: Counter[str] = field(default_factory=Counter)
    failure_by_extension: Counter[str] = field(default_factory=Counter)
    failure_by_filename_suffix: Counter[str] = field(default_factory=Counter)
    failure_by_mime_type: Counter[str] = field(default_factory=Counter)
    failure_groups: Counter[tuple[str, str, str, str, str]] = field(
        default_factory=Counter
    )
    failure_group_samples: dict[
        tuple[str, str, str, str, str], list[str]
    ] = field(default_factory=lambda: defaultdict(list))
    decompression_bomb_warning_count: int = 0
    decompression_bomb_warning_samples: list[str] = field(default_factory=list)

    def record_success(self, result: MediaProbeResult) -> None:
        self.scanned += 1
        self.probe_success += 1
        self.success_by_category[result.category.value.upper()] += 1

    def record_failure(
        self,
        item: ProbeInput,
        *,
        exception_class: str,
        reason: str,
        diagnostic_category: str,
    ) -> None:
        self.scanned += 1
        self.probe_failed += 1
        self.failure_by_exception[exception_class] += 1
        self.failure_by_reason[reason] += 1
        self.failure_by_diagnostic_category[diagnostic_category] += 1
        self.failure_by_extension[item.extension] += 1
        self.failure_by_filename_suffix[item.filename_suffix] += 1
        self.failure_by_mime_type[item.mime_type] += 1

        for service_name in item.service_names:
            self.failure_by_service[service_name] += 1
            key = (
                service_name,
                item.extension,
                item.mime_type,
                exception_class,
                reason,
            )
            self.failure_groups[key] += 1
            samples = self.failure_group_samples[key]
            if len(samples) < _MAX_SAMPLE_FILE_IDS and item.file_id not in samples:
                samples.append(item.file_id)

    def record_decompression_bomb_warning(self, file_id: str) -> None:
        self.decompression_bomb_warning_count += 1
        if (
            len(self.decompression_bomb_warning_samples) < _MAX_SAMPLE_FILE_IDS
            and file_id not in self.decompression_bomb_warning_samples
        ):
            self.decompression_bomb_warning_samples.append(file_id)

    def to_dict(
        self,
        *,
        limit: int | None,
        file_id: str | None,
    ) -> dict[str, object]:
        groups = []
        for key in sorted(self.failure_groups):
            service_name, extension, mime_type, exception_class, reason = key
            groups.append(
                {
                    "service_name": service_name,
                    "extension": extension,
                    "mime_type": mime_type,
                    "exception_class": exception_class,
                    "reason": reason,
                    "count": self.failure_groups[key],
                    "sample_file_ids": list(self.failure_group_samples[key]),
                }
            )

        return {
            "filters": {"limit": limit, "file_id": file_id},
            "scanned": self.scanned,
            "probe_success": self.probe_success,
            "probe_failed": self.probe_failed,
            "probe_success_by_category": _sorted_counter(self.success_by_category),
            "probe_failure_by_exception": _sorted_counter(
                self.failure_by_exception
            ),
            "probe_failure_by_reason": _sorted_counter(self.failure_by_reason),
            "probe_failure_by_diagnostic_category": _sorted_counter(
                self.failure_by_diagnostic_category
            ),
            "probe_failure_by_service": _sorted_counter(self.failure_by_service),
            "probe_failure_by_extension": _sorted_counter(
                self.failure_by_extension
            ),
            "probe_failure_by_filename_suffix": _sorted_counter(
                self.failure_by_filename_suffix
            ),
            "probe_failure_by_mime_type": _sorted_counter(
                self.failure_by_mime_type
            ),
            "probe_failure_groups": groups,
            "decompression_bomb_warnings": {
                "count": self.decompression_bomb_warning_count,
                "sample_file_ids": list(self.decompression_bomb_warning_samples),
            },
            "service_counting_note": (
                "Each CommonFile is probed once. A failure linked to multiple "
                "services is counted once per linked service only in service and "
                "combined-group projections."
            ),
        }


def diagnose_media_probe_failures(
    db: Session,
    *,
    storage_service: StorageService,
    media_probe: MediaProbe | None = None,
    limit: int | None = None,
    file_id: str | None = None,
) -> dict[str, object]:
    """Probe active CommonFile originals without mutating DB or storage."""
    probe = media_probe or MediaProbe()
    inputs = _load_probe_inputs(db, limit=limit, file_id=file_id)
    report = ProbeDiagnosticReport()

    for item in inputs:
        if not item.original_path:
            report.record_failure(
                item,
                exception_class="DiagnosticInputError",
                reason="missing original path",
                diagnostic_category="IO_FAILURE",
            )
            continue

        try:
            source = _resolve_original(storage_service, item.original_path)
        except (OSError, ValueError):
            report.record_failure(
                item,
                exception_class="DiagnosticInputError",
                reason="unsafe original path",
                diagnostic_category="IO_FAILURE",
            )
            continue

        if not source.is_file():
            report.record_failure(
                item,
                exception_class="DiagnosticInputError",
                reason="missing original file",
                diagnostic_category="IO_FAILURE",
            )
            continue

        caught_warnings: list[warnings.WarningMessage]
        result: MediaProbeResult | None = None
        failure: Exception | None = None
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always", Image.DecompressionBombWarning)
            try:
                result = probe.probe(source, filename=item.original_name)
            except Exception as exc:  # isolate every per-file probe failure
                failure = exc

        bomb_warnings = [
            entry
            for entry in caught_warnings
            if _is_decompression_bomb_warning(entry)
        ]
        for _entry in bomb_warnings:
            report.record_decompression_bomb_warning(item.file_id)

        if failure is not None:
            report.record_failure(
                item,
                exception_class=type(failure).__name__,
                reason=_safe_exception_reason(failure),
                diagnostic_category=classify_probe_failure(
                    failure,
                    filename_suffix=item.filename_suffix,
                ),
            )
            continue

        if result is None:
            report.record_failure(
                item,
                exception_class="UnexpectedProbeResult",
                reason="probe returned no result",
                diagnostic_category="OTHER",
            )
            continue
        report.record_success(result)

    return report.to_dict(limit=limit, file_id=file_id)


def classify_probe_failure(
    exc: Exception,
    *,
    filename_suffix: str = "",
) -> str:
    """Add a diagnostic category without replacing the original exception data."""
    if isinstance(exc, Image.DecompressionBombError):
        return "IMAGE_DECODE_FAILURE"
    if isinstance(exc, MediaToolUnavailableError):
        return "TOOL_UNAVAILABLE"
    if isinstance(exc, MediaCommandTimeout):
        return "TIMEOUT"
    if isinstance(exc, OSError):
        return "IO_FAILURE"

    reason = str(exc).casefold()
    if isinstance(exc, UnsupportedMediaError):
        if (
            "heic" in reason
            or "heif" in reason
            or filename_suffix in {".heic", ".heif"}
        ):
            return "HEIF_FAILURE"
        if reason == "invalid or unsupported video container":
            return "VIDEO_CONTAINER_UNSUPPORTED"
        if (
            "ffprobe" in reason
            or "video metadata" in reason
            or "video stream" in reason
        ):
            return "VIDEO_PROBE_FAILURE"
        if "does not exist" in reason or "unable to read" in reason:
            return "IO_FAILURE"
        return "LEGACY_OR_UNSUPPORTED_FORMAT"
    return "OTHER"


def _load_probe_inputs(
    db: Session,
    *,
    limit: int | None,
    file_id: str | None,
) -> list[ProbeInput]:
    """Load scalar scan inputs, then close the read transaction before probing."""
    db.rollback()
    try:
        _enforce_read_only_transaction(db)
        with db.no_autoflush:
            query = (
                db.query(
                    CommonFile.id,
                    CommonFile.file_id,
                    CommonFile.original_name,
                    CommonFile.original_path,
                    CommonFile.extension,
                    CommonFile.mime_type,
                )
                .filter(CommonFile.deleted.is_(False))
                .order_by(CommonFile.id.asc())
            )
            if file_id:
                query = query.filter(CommonFile.file_id == file_id)
            if limit is not None:
                query = query.limit(limit)
            rows = query.all()

            ids = [row.id for row in rows]
            service_names: dict[int, set[str]] = defaultdict(set)
            if ids:
                service_rows = (
                    db.query(
                        CommonFileService.file_id,
                        CommonFileService.service_name,
                    )
                    .filter(CommonFileService.file_id.in_(ids))
                    .order_by(
                        CommonFileService.file_id.asc(),
                        CommonFileService.service_name.asc(),
                    )
                    .all()
                )
                for service_row in service_rows:
                    normalized = str(service_row.service_name).strip()
                    if normalized:
                        service_names[service_row.file_id].add(normalized)

            return [
                ProbeInput(
                    common_file_id=row.id,
                    file_id=row.file_id,
                    original_name=row.original_name,
                    original_path=row.original_path,
                    extension=_normalize_value(row.extension),
                    mime_type=_normalize_value(row.mime_type),
                    filename_suffix=_normalize_value(Path(row.original_name).suffix),
                    service_names=tuple(sorted(service_names.get(row.id, set())))
                    or (_UNLINKED_SERVICE,),
                )
                for row in rows
            ]
    finally:
        db.rollback()


def _enforce_read_only_transaction(db: Session) -> None:
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.execute(text("SET TRANSACTION READ ONLY"))


def _resolve_original(storage_service: StorageService, stored_path: str) -> Path:
    original_root = storage_service.original_root.resolve(strict=False)
    resolved = storage_service.resolve_storage_path(stored_path).resolve(strict=False)
    try:
        resolved.relative_to(original_root)
    except ValueError as exc:
        raise ValueError("original path is outside configured storage") from exc
    return resolved


def _safe_exception_reason(exc: Exception) -> str:
    if isinstance(exc, Image.DecompressionBombError):
        return "image exceeds Pillow decompression-bomb safety limit"
    if isinstance(exc, MediaProbeError):
        reason = " ".join(str(exc).split())
        return reason[:240] if reason else type(exc).__name__
    if isinstance(exc, OSError):
        return "unexpected OSError"
    return "unexpected exception"


def _is_decompression_bomb_warning(entry: warnings.WarningMessage) -> bool:
    try:
        return issubclass(entry.category, Image.DecompressionBombWarning)
    except TypeError:
        return False


def _normalize_value(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    return normalized or _MISSING_VALUE


def _sorted_counter(counter: Counter[str]) -> dict[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only diagnostics for active CommonFile media probes",
    )
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to scan")
    parser.add_argument(
        "--file-id",
        default=None,
        help="Exact common_files.file_id lowercase SHA-256",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    if args.file_id and not _SHA256_RE.fullmatch(args.file_id):
        raise SystemExit("--file-id must be a lowercase SHA-256 digest")

    db = SessionLocal()
    try:
        result = diagnose_media_probe_failures(
            db,
            storage_service=StorageService(),
            limit=args.limit,
            file_id=args.file_id,
        )
    except Exception as exc:
        db.rollback()
        print(
            json.dumps(
                {"error": "diagnostic failed", "exception_class": type(exc).__name__},
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
