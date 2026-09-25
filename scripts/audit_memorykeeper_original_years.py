"""Read-only SHA-256 audit of NAS source years against MemoryKeeper dates."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import os
from pathlib import Path
import re
import sys
import time
from typing import Callable, Iterable, TextIO

from sqlalchemy import and_, text
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.place import MemoryKeeperPlace


SERVICE_NAME = "MemoryKeeper"
DEFAULT_ROOT = Path("/volume1/여행/여행사진")
DEFAULT_CHUNK_SIZE = 1024 * 1024
DEFAULT_PROGRESS_EVERY = 100
IGNORED_DIRECTORY_NAMES = frozenset({"@eadir", "#recycle"})

MATCH = "MATCH"
YEAR_MISMATCH = "YEAR_MISMATCH"
MISSING_EFFECTIVE_YEAR = "MISSING_EFFECTIVE_YEAR"
NOT_IN_MEMORYKEEPER = "NOT_IN_MEMORYKEEPER"
NO_PATH_YEAR = "NO_PATH_YEAR"
AMBIGUOUS_SOURCE_YEAR = "AMBIGUOUS_SOURCE_YEAR"
HASH_ERROR = "HASH_ERROR"

CSV_FIELDS = (
    "status",
    "original_path_year",
    "effective_capture_year",
    "effective_capture_date",
    "effective_capture_datetime",
    "date_basis",
    "user_capture_datetime",
    "user_capture_precision",
    "sha256",
    "original_path",
    "original_filename",
    "memorykeeper_place_id",
    "place_name",
    "date_revision",
    "source_duplicate_count",
    "error",
)


@dataclass(frozen=True)
class MemoryKeeperAuditRecord:
    effective_capture_year: int | None = None
    effective_capture_date: date | None = None
    effective_capture_datetime: datetime | None = None
    date_basis: str | None = None
    user_capture_datetime: datetime | None = None
    user_capture_precision: str | None = None
    memorykeeper_place_id: str | None = None
    place_name: str | None = None
    date_revision: int | None = None


@dataclass(frozen=True)
class HashedSource:
    scan_index: int
    path: Path
    original_path_year: int
    sha256: str


@dataclass(frozen=True)
class AuditRow:
    scan_index: int
    status: str
    original_path_year: int | None
    effective_capture_year: int | None = None
    effective_capture_date: date | None = None
    effective_capture_datetime: datetime | None = None
    date_basis: str | None = None
    user_capture_datetime: datetime | None = None
    user_capture_precision: str | None = None
    sha256: str | None = None
    original_path: str = ""
    original_filename: str = ""
    memorykeeper_place_id: str | None = None
    place_name: str | None = None
    date_revision: int | None = None
    source_duplicate_count: int = 1
    error: str | None = None

    def csv_dict(self) -> dict[str, object]:
        values = asdict(self)
        values.pop("scan_index")
        for field_name in (
            "effective_capture_date",
            "effective_capture_datetime",
            "user_capture_datetime",
        ):
            value = values[field_name]
            values[field_name] = value.isoformat() if value is not None else ""
        return {name: values.get(name, "") for name in CSV_FIELDS}


@dataclass
class AuditProgress:
    started_at: float
    scanned: int = 0
    hashed: int = 0
    matched: int = 0
    mismatch: int = 0
    missing_effective_year: int = 0
    not_in_memorykeeper: int = 0
    no_path_year: int = 0
    hash_errors: int = 0
    bytes_read: int = 0


HashFile = Callable[[Path], tuple[str, int]]


def load_active_memorykeeper_records(
    db: Session,
) -> dict[str, MemoryKeeperAuditRecord]:
    """Load one read-only snapshot keyed by the indexed SHA-256 identifier."""
    dialect = db.get_bind().dialect.name
    if dialect != "postgresql":
        raise RuntimeError(
            "MemoryKeeper original-year audit requires PostgreSQL so the "
            "transaction can be enforced READ ONLY"
        )

    # This must be the first SQL statement in the transaction.  No ORM object
    # is added or mutated and the caller always rolls the transaction back.
    db.execute(text("SET TRANSACTION READ ONLY"))
    rows = (
        db.query(
            CommonFile.file_id.label("sha256"),
            MemoryKeeperFileState.effective_capture_year,
            MemoryKeeperFileState.effective_capture_date,
            MemoryKeeperFileState.effective_capture_datetime,
            MemoryKeeperFileState.date_basis,
            MemoryKeeperFileState.user_capture_datetime,
            MemoryKeeperFileState.user_capture_precision,
            CommonFileMetadata.memorykeeper_place_id,
            CommonFileMetadata.place_name.label("metadata_place_name"),
            MemoryKeeperPlace.display_name.label("registered_place_name"),
            MemoryKeeperFileState.revision.label("date_revision"),
        )
        .select_from(CommonFile)
        .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
        .outerjoin(
            MemoryKeeperFileState,
            MemoryKeeperFileState.file_id == CommonFile.id,
        )
        .outerjoin(
            CommonFileMetadata,
            CommonFileMetadata.file_id == CommonFile.id,
        )
        .outerjoin(
            MemoryKeeperPlace,
            and_(
                MemoryKeeperPlace.id == CommonFileMetadata.memorykeeper_place_id,
                MemoryKeeperPlace.deleted_at.is_(None),
            ),
        )
        .filter(CommonFile.deleted.is_(False))
        .filter(CommonFileService.service_name == SERVICE_NAME)
        .all()
    )
    return {
        str(row.sha256).lower(): MemoryKeeperAuditRecord(
            effective_capture_year=(
                int(row.effective_capture_year)
                if row.effective_capture_year is not None
                else None
            ),
            effective_capture_date=row.effective_capture_date,
            effective_capture_datetime=row.effective_capture_datetime,
            date_basis=row.date_basis,
            user_capture_datetime=row.user_capture_datetime,
            user_capture_precision=row.user_capture_precision,
            memorykeeper_place_id=(
                str(row.memorykeeper_place_id)
                if row.memorykeeper_place_id is not None
                else None
            ),
            place_name=row.registered_place_name or row.metadata_place_name,
            date_revision=(
                int(row.date_revision) if row.date_revision is not None else None
            ),
        )
        for row in rows
    }


def iter_source_files(
    root: Path,
    *,
    scan_errors: list[str] | None = None,
) -> Iterable[Path]:
    """Yield source files deterministically without entering system folders."""
    errors = scan_errors if scan_errors is not None else []

    def on_error(error: OSError) -> None:
        errors.append(f"{type(error).__name__}: {error}")

    for current_root, directory_names, file_names in os.walk(
        root,
        topdown=True,
        onerror=on_error,
        followlinks=False,
    ):
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name.casefold() not in IGNORED_DIRECTORY_NAMES
        )
        base = Path(current_root)
        for file_name in sorted(file_names):
            yield base / file_name


def original_path_year(root: Path, path: Path) -> int | None:
    """Accept only ROOT/<exactly four digits>/... as a source year."""
    relative = path.relative_to(root)
    if len(relative.parts) < 2:
        return None
    first = relative.parts[0]
    if re.fullmatch(r"[0-9]{4}", first) is None:
        return None
    return int(first)


def sha256_file(
    path: Path,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> tuple[str, int]:
    """Hash a file with bounded memory and return the number of bytes read."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    bytes_read = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(chunk_size), b""):
            digest.update(chunk)
            bytes_read += len(chunk)
    return digest.hexdigest(), bytes_read


def audit_sources(
    *,
    root: Path,
    memorykeeper_records: dict[str, MemoryKeeperAuditRecord],
    progress_every: int = DEFAULT_PROGRESS_EVERY,
    max_files: int | None = None,
    hash_file: HashFile = sha256_file,
    progress_stream: TextIO = sys.stdout,
) -> tuple[list[AuditRow], AuditProgress, list[str]]:
    """Scan and classify source files without writing the source or database."""
    if progress_every <= 0:
        raise ValueError("progress_every must be positive")
    if max_files is not None and max_files <= 0:
        raise ValueError("max_files must be positive")

    progress = AuditProgress(started_at=time.monotonic())
    immediate_rows: list[AuditRow] = []
    hashed_sources: list[HashedSource] = []
    scan_errors: list[str] = []
    year_candidate_count = 0

    for scan_index, path in enumerate(
        iter_source_files(root, scan_errors=scan_errors),
        start=1,
    ):
        year = original_path_year(root, path)
        if (
            year is not None
            and max_files is not None
            and year_candidate_count >= max_files
        ):
            break
        progress.scanned += 1
        if year is None:
            progress.no_path_year += 1
            immediate_rows.append(
                AuditRow(
                    scan_index=scan_index,
                    status=NO_PATH_YEAR,
                    original_path_year=None,
                    original_path=str(path),
                    original_filename=path.name,
                )
            )
            _maybe_report_progress(progress, progress_every, progress_stream)
            continue

        year_candidate_count += 1
        try:
            digest, read_count = hash_file(path)
        except (OSError, ValueError) as exc:
            progress.hash_errors += 1
            immediate_rows.append(
                AuditRow(
                    scan_index=scan_index,
                    status=HASH_ERROR,
                    original_path_year=year,
                    original_path=str(path),
                    original_filename=path.name,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            _maybe_report_progress(progress, progress_every, progress_stream)
            continue

        digest = digest.lower()
        progress.hashed += 1
        progress.bytes_read += read_count
        hashed_sources.append(
            HashedSource(
                scan_index=scan_index,
                path=path,
                original_path_year=year,
                sha256=digest,
            )
        )
        record = memorykeeper_records.get(digest)
        if record is None:
            progress.not_in_memorykeeper += 1
        elif record.effective_capture_year is None:
            progress.missing_effective_year += 1
        elif record.effective_capture_year == year:
            progress.matched += 1
        else:
            progress.mismatch += 1
        _maybe_report_progress(progress, progress_every, progress_stream)

    rows = immediate_rows + _classify_hashed_sources(
        hashed_sources,
        memorykeeper_records=memorykeeper_records,
    )
    rows.sort(key=lambda row: row.scan_index)
    _print_progress(progress, progress_stream, final=True)
    return rows, progress, scan_errors


def _classify_hashed_sources(
    sources: list[HashedSource],
    *,
    memorykeeper_records: dict[str, MemoryKeeperAuditRecord],
) -> list[AuditRow]:
    by_sha: dict[str, list[HashedSource]] = defaultdict(list)
    for source in sources:
        by_sha[source.sha256].append(source)

    result: list[AuditRow] = []
    for digest, duplicates in by_sha.items():
        years = {source.original_path_year for source in duplicates}
        record = memorykeeper_records.get(digest)
        for source in duplicates:
            if len(years) > 1:
                status = AMBIGUOUS_SOURCE_YEAR
            elif record is None:
                status = NOT_IN_MEMORYKEEPER
            elif record.effective_capture_year is None:
                status = MISSING_EFFECTIVE_YEAR
            elif record.effective_capture_year == source.original_path_year:
                status = MATCH
            else:
                status = YEAR_MISMATCH
            result.append(
                _audit_row(
                    source=source,
                    status=status,
                    duplicate_count=len(duplicates),
                    record=record,
                )
            )
    return result


def _audit_row(
    *,
    source: HashedSource,
    status: str,
    duplicate_count: int,
    record: MemoryKeeperAuditRecord | None,
) -> AuditRow:
    values = record or MemoryKeeperAuditRecord()
    return AuditRow(
        scan_index=source.scan_index,
        status=status,
        original_path_year=source.original_path_year,
        effective_capture_year=values.effective_capture_year,
        effective_capture_date=values.effective_capture_date,
        effective_capture_datetime=values.effective_capture_datetime,
        date_basis=values.date_basis,
        user_capture_datetime=values.user_capture_datetime,
        user_capture_precision=values.user_capture_precision,
        sha256=source.sha256,
        original_path=str(source.path),
        original_filename=source.path.name,
        memorykeeper_place_id=values.memorykeeper_place_id,
        place_name=values.place_name,
        date_revision=values.date_revision,
        source_duplicate_count=duplicate_count,
    )


def write_csv(rows: Iterable[AuditRow], output_path: Path) -> None:
    """Create a new Excel-compatible UTF-8 BOM CSV without overwriting."""
    with output_path.open("x", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.csv_dict())


def _maybe_report_progress(
    progress: AuditProgress,
    progress_every: int,
    stream: TextIO,
) -> None:
    if progress.scanned % progress_every == 0:
        _print_progress(progress, stream, final=False)


def _print_progress(
    progress: AuditProgress,
    stream: TextIO,
    *,
    final: bool,
) -> None:
    elapsed = max(time.monotonic() - progress.started_at, 0.0)
    prefix = "scan_complete" if final else "progress"
    print(
        f"{prefix} scanned={progress.scanned} hashed={progress.hashed} "
        f"matched={progress.matched} mismatch={progress.mismatch} "
        f"missing_effective_year={progress.missing_effective_year} "
        f"not_in_memorykeeper={progress.not_in_memorykeeper} "
        f"no_path_year={progress.no_path_year} hash_errors={progress.hash_errors} "
        f"bytes_read={progress.bytes_read} elapsed_seconds={elapsed:.1f}",
        file=stream,
        flush=True,
    )


def _print_summary(
    rows: list[AuditRow],
    *,
    active_memorykeeper_count: int,
    scan_errors: list[str],
    output_path: Path,
) -> None:
    counts = Counter(row.status for row in rows)
    duplicate_hashes = {
        row.sha256
        for row in rows
        if row.sha256 and row.source_duplicate_count > 1
    }
    ambiguous_hashes = {
        row.sha256 for row in rows if row.status == AMBIGUOUS_SOURCE_YEAR
    }
    print("summary")
    print(f"active_memorykeeper_files={active_memorykeeper_count}")
    print(f"detail_rows={len(rows)}")
    for status in (
        MATCH,
        YEAR_MISMATCH,
        MISSING_EFFECTIVE_YEAR,
        NOT_IN_MEMORYKEEPER,
        NO_PATH_YEAR,
        AMBIGUOUS_SOURCE_YEAR,
        HASH_ERROR,
    ):
        print(f"{status}={counts[status]}")
    print(f"duplicate_sha256_groups={len(duplicate_hashes)}")
    print(f"ambiguous_sha256_groups={len(ambiguous_hashes)}")
    print(f"directory_scan_errors={len(scan_errors)}")
    for error in scan_errors:
        print(f"directory_scan_error={error}", file=sys.stderr)
    print(f"csv={output_path}")


def _validate_paths(root: Path, output_path: Path) -> tuple[Path, Path]:
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise ValueError(f"source root is not a directory: {resolved_root}")
    resolved_output = output_path.resolve(strict=False)
    if not resolved_output.parent.is_dir():
        raise ValueError(f"output parent does not exist: {resolved_output.parent}")
    try:
        resolved_output.relative_to(resolved_root)
    except ValueError:
        pass
    else:
        raise ValueError("output CSV must not be created inside the source root")
    if resolved_output.exists():
        raise FileExistsError(f"output CSV already exists: {resolved_output}")
    return resolved_root, resolved_output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only SHA-256 comparison of NAS path years and MemoryKeeper "
            "effective capture years"
        )
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--progress-every",
        type=int,
        default=DEFAULT_PROGRESS_EVERY,
    )
    parser.add_argument(
        "--max-files",
        type=_positive_int,
        default=None,
        help="Stop after attempting this many files under valid YYYY folders",
    )
    return parser


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root, output_path = _validate_paths(args.root, args.output)
    if args.progress_every <= 0:
        raise ValueError("--progress-every must be positive")

    # Importing runtime DB configuration is intentionally delayed so unit tests
    # for path/hash/classification logic never need a database connection.
    from app.common.database import SessionLocal

    db = SessionLocal()
    try:
        memorykeeper_records = load_active_memorykeeper_records(db)
    finally:
        # Never commit from this diagnostic.  Closing the read-only transaction
        # before hashing also avoids holding an idle DB snapshot for many hours.
        db.rollback()
        db.close()

    print(f"active_memorykeeper_snapshot={len(memorykeeper_records)}")
    rows, _progress, scan_errors = audit_sources(
        root=root,
        memorykeeper_records=memorykeeper_records,
        progress_every=args.progress_every,
        max_files=args.max_files,
    )
    write_csv(rows, output_path)
    _print_summary(
        rows,
        active_memorykeeper_count=len(memorykeeper_records),
        scan_errors=scan_errors,
        output_path=output_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
