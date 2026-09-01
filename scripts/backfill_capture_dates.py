"""Resumable MemoryKeeper capture-date backfill.

The default mode is dry-run.  It reads only active MemoryKeeper links and
never uses legacy ``datetime_original`` as a capture-date source.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.database import SessionLocal
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.repositories.metadata_repository import MetadataRepository
from app.common.services.photo_analysis import ExifReader
from app.common.services.storage_service import StorageService
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.services.capture_date_service import (
    CaptureDateBasis,
    CaptureDateProjection,
    MemoryKeeperCaptureDateService,
    calculate_capture_date_projection,
)
from scripts.backfill_exif_metadata import _resolve_original_path


SERVICE_NAME = "MemoryKeeper"
DEFAULT_BATCH_SIZE = 50
MAX_BATCH_SIZE = 50
FailureReporter = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class CaptureDateSnapshot:
    common_file_id: int
    public_file_id: str
    original_path: str | None
    common_file_favorite: bool
    file_created_at: datetime | None
    service_created_at: datetime | None
    original_capture_datetime: datetime | None
    legacy_datetime_original: datetime | None
    state_exists: bool
    user_capture_datetime: datetime | None
    user_capture_precision: str | None
    effective_capture_datetime: datetime | None
    effective_capture_date: date | None
    effective_capture_year: int | None
    date_basis: str | None
    revision: int | None


@dataclass(frozen=True)
class ExifScanResult:
    capture_datetime: datetime | None = None
    reason: str | None = None


@dataclass
class BackfillStats:
    scanned: int = 0
    state_missing: int = 0
    user: int = 0
    original_exif_available: int = 0
    original_file_reextractable: int = 0
    original_file_missing: int = 0
    unsafe_path: int = 0
    read_error: int = 0
    no_capture_datetime: int = 0
    would_set_original_capture_datetime: int = 0
    would_use_user: int = 0
    would_use_exif: int = 0
    would_use_imported: int = 0
    would_use_created: int = 0
    no_source: int = 0
    projection_mismatch: int = 0
    generated_mismatch: int = 0
    would_update: int = 0
    updated: int = 0
    skipped_inactive: int = 0
    failed: int = 0
    high_water_common_file_id: int | None = None
    last_common_file_id: int | None = None


@dataclass
class ValidationStats:
    active_memorykeeper_links: int = 0
    state_present: int = 0
    state_missing: int = 0
    original_capture_present: int = 0
    user_capture_present: int = 0
    effective_capture_present: int = 0
    effective_capture_null: int = 0
    basis_user: int = 0
    basis_exif: int = 0
    basis_imported: int = 0
    basis_created: int = 0
    basis_null: int = 0
    basis_invalid: int = 0
    imported_at_null: int = 0
    file_created_at_null: int = 0
    legacy_datetime_present: int = 0
    legacy_and_original_present: int = 0
    orphan_states: int = 0
    deleted_memorykeeper_links: int = 0
    duplicate_memorykeeper_link_groups: int = 0
    effective_mismatch: int = 0
    basis_mismatch: int = 0
    source_but_effective_null: int = 0
    generated_date_mismatch: int = 0
    generated_year_mismatch: int = 0

    @property
    def is_clean(self) -> bool:
        return all(
            value == 0
            for value in (
                self.state_missing,
                self.effective_mismatch,
                self.basis_mismatch,
                self.source_but_effective_null,
                self.generated_date_mismatch,
                self.generated_year_mismatch,
            )
        )


def backfill_capture_dates(
    db: Session,
    *,
    storage_service: StorageService,
    exif_reader: ExifReader | None = None,
    execute: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_rows: int | None = None,
    after_file_id: int = 0,
    projection_only: bool = False,
    failure_reporter: FailureReporter | None = None,
) -> BackfillStats:
    """Backfill active MemoryKeeper capture-date state in bounded batches."""
    _validate_options(
        batch_size=batch_size,
        max_rows=max_rows,
        after_file_id=after_file_id,
    )
    reader = exif_reader or ExifReader()
    stats = BackfillStats()
    high_water = _active_high_water(db)
    stats.high_water_common_file_id = high_water
    if high_water is None or high_water <= after_file_id:
        return stats

    cursor = after_file_id
    remaining = max_rows
    while cursor < high_water and (remaining is None or remaining > 0):
        limit = batch_size if remaining is None else min(batch_size, remaining)
        snapshots = _load_snapshot_batch(
            db,
            after_file_id=cursor,
            through_file_id=high_water,
            limit=limit,
        )
        # End the read transaction before touching NAS storage.
        db.rollback()
        if not snapshots:
            break

        scanned: list[tuple[CaptureDateSnapshot, ExifScanResult]] = []
        for snapshot in snapshots:
            scan = _scan_original(
                snapshot,
                storage_service=storage_service,
                exif_reader=reader,
                projection_only=projection_only,
            )
            _record_dry_run_row(
                stats,
                snapshot=snapshot,
                scan=scan,
                postgres=_is_postgresql(db),
            )
            if scan.reason in {
                "ORIGINAL_FILE_MISSING",
                "UNSAFE_PATH",
                "READ_ERROR",
                "NO_CAPTURE_DATETIME",
            }:
                _report_failure(failure_reporter, snapshot, scan.reason)
            scanned.append((snapshot, scan))
            cursor = snapshot.common_file_id
            stats.last_common_file_id = cursor
            if remaining is not None:
                remaining -= 1

        if execute:
            for snapshot, scan in scanned:
                try:
                    # A failed row rolls back only its savepoint; the remaining
                    # bounded batch continues and commits normally.
                    with db.begin_nested():
                        changed, inactive = _apply_snapshot(
                            db,
                            snapshot=snapshot,
                            scan=scan,
                        )
                    if inactive:
                        stats.skipped_inactive += 1
                    elif changed:
                        stats.updated += 1
                except Exception as exc:  # pragma: no cover - defensive CLI path
                    stats.failed += 1
                    _report_failure(
                        failure_reporter,
                        snapshot,
                        f"WRITE_ERROR:{type(exc).__name__}",
                    )
            try:
                db.commit()
            except Exception:
                db.rollback()
                raise

    return stats


def validate_capture_dates(db: Session) -> ValidationStats:
    """Return the capture-date transition gate without mutating state."""
    stats = ValidationStats()
    postgres = _is_postgresql(db)
    for snapshot in _iter_active_snapshots(db):
        stats.active_memorykeeper_links += 1
        if snapshot.state_exists:
            stats.state_present += 1
        else:
            stats.state_missing += 1
        if snapshot.original_capture_datetime is not None:
            stats.original_capture_present += 1
        if snapshot.user_capture_datetime is not None:
            stats.user_capture_present += 1
        if snapshot.effective_capture_datetime is not None:
            stats.effective_capture_present += 1
        else:
            stats.effective_capture_null += 1
        _increment_basis_count(stats, snapshot.date_basis)
        if snapshot.service_created_at is None:
            stats.imported_at_null += 1
        if snapshot.file_created_at is None:
            stats.file_created_at_null += 1
        if snapshot.legacy_datetime_original is not None:
            stats.legacy_datetime_present += 1
        if (
            snapshot.legacy_datetime_original is not None
            and snapshot.original_capture_datetime is not None
        ):
            stats.legacy_and_original_present += 1

        projection = _projection_for_snapshot(snapshot, original_candidate=None)
        if snapshot.state_exists:
            if snapshot.effective_capture_datetime != projection.effective_capture_datetime:
                stats.effective_mismatch += 1
            if snapshot.date_basis != projection.date_basis:
                stats.basis_mismatch += 1
            if (
                projection.effective_capture_datetime is not None
                and snapshot.effective_capture_datetime is None
            ):
                stats.source_but_effective_null += 1
            if postgres:
                if snapshot.effective_capture_date != _expected_date(snapshot):
                    stats.generated_date_mismatch += 1
                if snapshot.effective_capture_year != _expected_year(snapshot):
                    stats.generated_year_mismatch += 1

    stats.orphan_states = _orphan_state_count(db)
    stats.deleted_memorykeeper_links = _deleted_link_count(db)
    stats.duplicate_memorykeeper_link_groups = _duplicate_link_group_count(db)
    return stats


def _active_high_water(db: Session) -> int | None:
    return (
        db.query(func.max(CommonFile.id))
        .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
        .filter(CommonFileService.service_name == SERVICE_NAME)
        .filter(CommonFile.deleted.is_(False))
        .scalar()
    )


def _load_snapshot_batch(
    db: Session,
    *,
    after_file_id: int,
    through_file_id: int,
    limit: int,
) -> list[CaptureDateSnapshot]:
    rows = (
        _active_snapshot_query(db)
        .filter(CommonFile.id > after_file_id)
        .filter(CommonFile.id <= through_file_id)
        .order_by(CommonFile.id.asc())
        .limit(limit)
        .all()
    )
    return [_snapshot_from_row(row) for row in rows]


def _iter_active_snapshots(db: Session) -> Iterator[CaptureDateSnapshot]:
    for row in _active_snapshot_query(db).order_by(CommonFile.id.asc()).yield_per(500):
        yield _snapshot_from_row(row)


def _active_snapshot_query(db: Session):
    return (
        db.query(CommonFile, CommonFileService, CommonFileMetadata, MemoryKeeperFileState)
        .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
        .outerjoin(CommonFileMetadata, CommonFileMetadata.file_id == CommonFile.id)
        .outerjoin(MemoryKeeperFileState, MemoryKeeperFileState.file_id == CommonFile.id)
        .filter(CommonFileService.service_name == SERVICE_NAME)
        .filter(CommonFile.deleted.is_(False))
    )


def _snapshot_from_row(
    row: tuple[
        CommonFile,
        CommonFileService,
        CommonFileMetadata | None,
        MemoryKeeperFileState | None,
    ],
) -> CaptureDateSnapshot:
    common_file, service_link, metadata, state = row
    return CaptureDateSnapshot(
        common_file_id=common_file.id,
        public_file_id=common_file.file_id,
        original_path=common_file.original_path,
        common_file_favorite=bool(common_file.favorite),
        file_created_at=common_file.created_at,
        service_created_at=service_link.created_at,
        original_capture_datetime=(
            metadata.original_capture_datetime if metadata is not None else None
        ),
        legacy_datetime_original=(
            metadata.datetime_original if metadata is not None else None
        ),
        state_exists=state is not None,
        user_capture_datetime=(
            state.user_capture_datetime if state is not None else None
        ),
        user_capture_precision=(
            state.user_capture_precision if state is not None else None
        ),
        effective_capture_datetime=(
            state.effective_capture_datetime if state is not None else None
        ),
        effective_capture_date=(
            state.effective_capture_date if state is not None else None
        ),
        effective_capture_year=(
            state.effective_capture_year if state is not None else None
        ),
        date_basis=state.date_basis if state is not None else None,
        revision=state.revision if state is not None else None,
    )


def _scan_original(
    snapshot: CaptureDateSnapshot,
    *,
    storage_service: StorageService,
    exif_reader: ExifReader,
    projection_only: bool,
) -> ExifScanResult:
    if snapshot.original_capture_datetime is not None:
        return ExifScanResult(reason="ORIGINAL_EXIF_AVAILABLE")
    if projection_only:
        return ExifScanResult(reason="PROJECTION_ONLY")
    if not snapshot.original_path:
        return ExifScanResult(reason="ORIGINAL_FILE_MISSING")
    try:
        original_path = _resolve_original_path(storage_service, snapshot.original_path)
    except ValueError:
        return ExifScanResult(reason="UNSAFE_PATH")
    except OSError:
        return ExifScanResult(reason="READ_ERROR")
    if not original_path.is_file():
        return ExifScanResult(reason="ORIGINAL_FILE_MISSING")
    try:
        extracted = exif_reader.read(original_path)
    except Exception:
        return ExifScanResult(reason="READ_ERROR")
    value = extracted.get("datetime_original")
    if value is None:
        return ExifScanResult(reason="NO_CAPTURE_DATETIME")
    if value.tzinfo is not None and value.utcoffset() is not None:
        return ExifScanResult(reason="READ_ERROR")
    return ExifScanResult(capture_datetime=value, reason="ORIGINAL_FILE_REEXTRACTABLE")


def _record_dry_run_row(
    stats: BackfillStats,
    *,
    snapshot: CaptureDateSnapshot,
    scan: ExifScanResult,
    postgres: bool,
) -> None:
    stats.scanned += 1
    if not snapshot.state_exists:
        stats.state_missing += 1
    if snapshot.user_capture_datetime is not None:
        stats.user += 1
    if snapshot.original_capture_datetime is not None:
        stats.original_exif_available += 1
    elif scan.reason == "ORIGINAL_FILE_REEXTRACTABLE":
        stats.original_file_reextractable += 1
        stats.would_set_original_capture_datetime += 1
    elif scan.reason == "ORIGINAL_FILE_MISSING":
        stats.original_file_missing += 1
    elif scan.reason == "UNSAFE_PATH":
        stats.unsafe_path += 1
    elif scan.reason == "READ_ERROR":
        stats.read_error += 1
    elif scan.reason == "NO_CAPTURE_DATETIME":
        stats.no_capture_datetime += 1

    projection = _projection_for_snapshot(
        snapshot,
        original_candidate=scan.capture_datetime,
    )
    _increment_would_use(stats, projection)
    projection_mismatch = (
        snapshot.state_exists
        and (
            snapshot.effective_capture_datetime != projection.effective_capture_datetime
            or snapshot.date_basis != projection.date_basis
        )
    )
    if projection_mismatch:
        stats.projection_mismatch += 1
    generated_mismatch = _snapshot_generated_mismatch(snapshot, postgres=postgres)
    if generated_mismatch:
        stats.generated_mismatch += 1
    if (
        not snapshot.state_exists
        or scan.capture_datetime is not None
        or projection_mismatch
        or generated_mismatch
    ):
        stats.would_update += 1


def _apply_snapshot(
    db: Session,
    *,
    snapshot: CaptureDateSnapshot,
    scan: ExifScanResult,
) -> tuple[bool, bool]:
    current = _load_current_active_row(db, common_file_id=snapshot.common_file_id)
    if current is None:
        return False, True
    common_file, service_link, metadata, state = current

    original_changed = False
    if scan.capture_datetime is not None:
        if metadata is None:
            metadata = _get_or_create_metadata(db, common_file.id)
        if metadata.original_capture_datetime is None:
            MetadataRepository(db).set_original_capture_datetime_if_missing(
                item=metadata,
                value=scan.capture_datetime,
                modified_by="backfill_capture_dates.py",
                commit=False,
            )
            original_changed = True

    state, state_created = _get_or_create_state(
        db,
        common_file=common_file,
        state=state,
    )
    projection = calculate_capture_date_projection(
        user_capture_datetime=state.user_capture_datetime,
        original_capture_datetime=(
            metadata.original_capture_datetime if metadata is not None else None
        ),
        imported_at=service_link.created_at,
        created_at=common_file.created_at,
    )
    projection_mismatch = not _state_matches_projection(state, projection)
    generated_mismatch = _state_generated_mismatch(state, postgres=_is_postgresql(db))
    if state_created or original_changed or projection_mismatch:
        MemoryKeeperCaptureDateService(db).synchronize(
            common_file=common_file,
            service_link=service_link,
            metadata=metadata,
            state=state,
        )
    if generated_mismatch and not projection_mismatch:
        # A generated-column mismatch should be impossible in a healthy
        # PostgreSQL table.  A no-op source-column UPDATE asks PostgreSQL to
        # recompute the stored dependants without assigning them directly.
        db.execute(
            update(MemoryKeeperFileState)
            .where(MemoryKeeperFileState.file_id == common_file.id)
            .values(
                effective_capture_datetime=projection.effective_capture_datetime,
                date_basis=projection.date_basis,
            )
        )
    return state_created or original_changed or projection_mismatch or generated_mismatch, False


def _load_current_active_row(
    db: Session,
    *,
    common_file_id: int,
) -> tuple[
    CommonFile,
    CommonFileService,
    CommonFileMetadata | None,
    MemoryKeeperFileState | None,
] | None:
    common_file = (
        db.query(CommonFile)
        .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
        .filter(CommonFile.id == common_file_id)
        .filter(CommonFileService.service_name == SERVICE_NAME)
        .filter(CommonFile.deleted.is_(False))
        .with_for_update()
        .first()
    )
    if common_file is None:
        return None
    service_link = (
        db.query(CommonFileService)
        .filter(CommonFileService.file_id == common_file.id)
        .filter(CommonFileService.service_name == SERVICE_NAME)
        .with_for_update()
        .first()
    )
    if service_link is None:
        return None
    metadata = (
        db.query(CommonFileMetadata)
        .filter(CommonFileMetadata.file_id == common_file.id)
        .with_for_update()
        .first()
    )
    state = (
        db.query(MemoryKeeperFileState)
        .filter(MemoryKeeperFileState.file_id == common_file.id)
        .with_for_update()
        .first()
    )
    return common_file, service_link, metadata, state


def _get_or_create_metadata(db: Session, common_file_id: int) -> CommonFileMetadata:
    existing = (
        db.query(CommonFileMetadata)
        .filter(CommonFileMetadata.file_id == common_file_id)
        .with_for_update()
        .first()
    )
    if existing is not None:
        return existing
    try:
        with db.begin_nested():
            created = CommonFileMetadata(file_id=common_file_id)
            db.add(created)
            db.flush()
    except IntegrityError:
        existing = (
            db.query(CommonFileMetadata)
            .filter(CommonFileMetadata.file_id == common_file_id)
            .with_for_update()
            .first()
        )
        if existing is None:
            raise
        return existing
    return created


def _get_or_create_state(
    db: Session,
    *,
    common_file: CommonFile,
    state: MemoryKeeperFileState | None,
) -> tuple[MemoryKeeperFileState, bool]:
    if state is not None:
        return state, False
    try:
        with db.begin_nested():
            created = MemoryKeeperFileState(
                file_id=common_file.id,
                favorite=bool(common_file.favorite),
                memo=None,
                revision=0,
            )
            db.add(created)
            db.flush()
    except IntegrityError:
        existing = (
            db.query(MemoryKeeperFileState)
            .filter(MemoryKeeperFileState.file_id == common_file.id)
            .with_for_update()
            .first()
        )
        if existing is None:
            raise
        return existing, False
    return created, True


def _projection_for_snapshot(
    snapshot: CaptureDateSnapshot,
    *,
    original_candidate: datetime | None,
) -> CaptureDateProjection:
    return calculate_capture_date_projection(
        user_capture_datetime=snapshot.user_capture_datetime,
        original_capture_datetime=(
            snapshot.original_capture_datetime or original_candidate
        ),
        imported_at=snapshot.service_created_at,
        created_at=snapshot.file_created_at,
    )


def _state_matches_projection(
    state: MemoryKeeperFileState,
    projection: CaptureDateProjection,
) -> bool:
    return (
        state.effective_capture_datetime == projection.effective_capture_datetime
        and state.date_basis == projection.date_basis
    )


def _snapshot_generated_mismatch(
    snapshot: CaptureDateSnapshot,
    *,
    postgres: bool,
) -> bool:
    # SQLite's ordinary unit-test schema has nullable stand-ins for the
    # PostgreSQL generated columns, so only PostgreSQL validation checks them.
    if not postgres:
        return False
    return (
        snapshot.effective_capture_date != _expected_date(snapshot)
        or snapshot.effective_capture_year != _expected_year(snapshot)
    )


def _state_generated_mismatch(
    state: MemoryKeeperFileState,
    *,
    postgres: bool,
) -> bool:
    if not postgres:
        return False
    expected_date = state.effective_capture_datetime.date() if state.effective_capture_datetime else None
    expected_year = state.effective_capture_datetime.year if state.effective_capture_datetime else None
    return (
        state.effective_capture_date != expected_date
        or state.effective_capture_year != expected_year
    )


def _expected_date(snapshot: CaptureDateSnapshot) -> date | None:
    return (
        snapshot.effective_capture_datetime.date()
        if snapshot.effective_capture_datetime is not None
        else None
    )


def _expected_year(snapshot: CaptureDateSnapshot) -> int | None:
    return (
        snapshot.effective_capture_datetime.year
        if snapshot.effective_capture_datetime is not None
        else None
    )


def _increment_would_use(stats: BackfillStats, projection: CaptureDateProjection) -> None:
    if projection.date_basis == CaptureDateBasis.USER:
        stats.would_use_user += 1
    elif projection.date_basis == CaptureDateBasis.EXIF:
        stats.would_use_exif += 1
    elif projection.date_basis == CaptureDateBasis.IMPORTED:
        stats.would_use_imported += 1
    elif projection.date_basis == CaptureDateBasis.CREATED:
        stats.would_use_created += 1
    else:
        stats.no_source += 1


def _increment_basis_count(stats: ValidationStats, basis: str | None) -> None:
    if basis == CaptureDateBasis.USER:
        stats.basis_user += 1
    elif basis == CaptureDateBasis.EXIF:
        stats.basis_exif += 1
    elif basis == CaptureDateBasis.IMPORTED:
        stats.basis_imported += 1
    elif basis == CaptureDateBasis.CREATED:
        stats.basis_created += 1
    elif basis is None:
        stats.basis_null += 1
    else:
        stats.basis_invalid += 1


def _orphan_state_count(db: Session) -> int:
    return (
        db.query(MemoryKeeperFileState)
        .outerjoin(CommonFile, CommonFile.id == MemoryKeeperFileState.file_id)
        .outerjoin(
            CommonFileService,
            (CommonFileService.file_id == MemoryKeeperFileState.file_id)
            & (CommonFileService.service_name == SERVICE_NAME),
        )
        .filter(
            or_(
                CommonFile.id.is_(None),
                CommonFile.deleted.is_not(False),
                CommonFileService.id.is_(None),
            )
        )
        .count()
    )


def _deleted_link_count(db: Session) -> int:
    return (
        db.query(CommonFileService)
        .join(CommonFile, CommonFile.id == CommonFileService.file_id)
        .filter(CommonFileService.service_name == SERVICE_NAME)
        .filter(CommonFile.deleted.is_(True))
        .count()
    )


def _duplicate_link_group_count(db: Session) -> int:
    return (
        db.query(CommonFileService.file_id)
        .filter(CommonFileService.service_name == SERVICE_NAME)
        .group_by(CommonFileService.file_id)
        .having(func.count(CommonFileService.id) > 1)
        .count()
    )


def _is_postgresql(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def _report_failure(
    reporter: FailureReporter | None,
    snapshot: CaptureDateSnapshot,
    reason: str | None,
) -> None:
    if reporter is None:
        return
    reporter(
        {
            "event": "failure",
            "common_file_id": snapshot.common_file_id,
            "file_id": snapshot.public_file_id,
            "reason": reason or "UNKNOWN",
        }
    )


def _validate_options(
    *,
    batch_size: int,
    max_rows: int | None,
    after_file_id: int,
) -> None:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must not exceed {MAX_BATCH_SIZE}")
    if max_rows is not None and max_rows < 1:
        raise ValueError("max_rows must be at least 1")
    if after_file_id < 0:
        raise ValueError("after_file_id must be zero or greater")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill MemoryKeeper capture-date state from original assets",
    )
    write_mode = parser.add_mutually_exclusive_group()
    write_mode.add_argument("--dry-run", action="store_true", help="Read/report only (default)")
    write_mode.add_argument("--execute", action="store_true", help="Persist capture-date updates")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument(
        "--projection-only",
        action="store_true",
        help="Skip filesystem EXIF reads and reconcile current DB sources only",
    )
    operation.add_argument(
        "--validate-only",
        action="store_true",
        help="Report the fast-read capture-date transition gate without writes",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--after-file-id", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.validate_only and (args.execute or args.dry_run):
        raise SystemExit("--validate-only cannot be combined with --dry-run or --execute")
    try:
        _validate_options(
            batch_size=args.batch_size,
            max_rows=args.max_rows,
            after_file_id=args.after_file_id,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    db = SessionLocal()
    try:
        if args.validate_only:
            validation = validate_capture_dates(db)
            print(json.dumps({"mode": "validate-only", **asdict(validation)}, default=str))
            return 0 if validation.is_clean else 2

        def report(item: dict[str, object]) -> None:
            print(json.dumps(item, default=str, ensure_ascii=False))

        stats = backfill_capture_dates(
            db,
            storage_service=StorageService(),
            execute=bool(args.execute),
            batch_size=args.batch_size,
            max_rows=args.max_rows,
            after_file_id=args.after_file_id,
            projection_only=bool(args.projection_only),
            failure_reporter=report,
        )
        print(
            json.dumps(
                {
                    "mode": "execute" if args.execute else "dry-run",
                    "projection_only": bool(args.projection_only),
                    **asdict(stats),
                },
                default=str,
            )
        )
        return 0 if stats.failed == 0 else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
