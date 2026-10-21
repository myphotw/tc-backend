"""Repair missing GPS projection for one explicitly identified CommonFile.

Dry-run is the default. The command reads one managed original and refuses to
write when either GPS field has a latest USER history entry.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import re

from sqlalchemy.orm import Session

from app.common.database import SessionLocal
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.repositories.history_repository import HistoryRepository
from app.common.repositories.metadata_repository import MetadataRepository, MetadataSource
from app.common.services.photo_analysis import ExifReader
from app.common.services.storage_service import StorageService
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService


@dataclass(frozen=True)
class RepairResult:
    common_file_id: int
    sha256: str
    mode: str
    status: str
    place_matched: bool = False


def repair_missing_gps_metadata(
    db: Session,
    *,
    storage_service: StorageService,
    common_file_id: int,
    expected_sha256: str,
    exif_reader: ExifReader | None = None,
    execute: bool = False,
    match_place: bool = False,
) -> RepairResult:
    """Inspect or repair exactly one file after validating both ID and SHA."""
    expected_sha256 = expected_sha256.lower()
    common_file, metadata = _load_target(
        db,
        common_file_id=common_file_id,
        expected_sha256=expected_sha256,
        lock=execute,
    )
    _require_memorykeeper_link(db, common_file_id)
    _require_missing_gps(metadata)
    _reject_user_gps_history(db, common_file_id)

    if not common_file.original_path:
        raise ValueError("target CommonFile has no original_path")
    original_path = storage_service.resolve_original_path(common_file.original_path)
    if not original_path.is_file():
        raise ValueError("target original file does not exist")

    extracted = (exif_reader or ExifReader()).read(original_path)
    latitude = extracted.get("gps_lat")
    longitude = extracted.get("gps_lon")
    if latitude is None or longitude is None:
        raise ValueError("target original has no complete GPS EXIF coordinates")

    if not execute:
        return RepairResult(
            common_file_id=common_file_id,
            sha256=expected_sha256,
            mode="dry-run",
            status="WOULD_REPAIR",
        )

    try:
        restored = MetadataRepository(db).upsert_fields(
            file_id=common_file_id,
            values={"gps_lat": latitude, "gps_lon": longitude},
            source=MetadataSource.GPS,
            modified_by="repair_missing_gps_metadata.py",
            commit=False,
            restore_missing_projection=True,
        )
        if restored.gps_lat is None or restored.gps_lon is None:
            raise RuntimeError("GPS projection repair was not persisted")

        place_matched = False
        if match_place:
            place_matched = MemoryKeeperPlaceService(db).auto_match_file(
                file_id=common_file_id,
                create_missing=False,
            )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return RepairResult(
        common_file_id=common_file_id,
        sha256=expected_sha256,
        mode="execute",
        status="REPAIRED",
        place_matched=place_matched,
    )


def _load_target(
    db: Session,
    *,
    common_file_id: int,
    expected_sha256: str,
    lock: bool,
) -> tuple[CommonFile, CommonFileMetadata]:
    query = db.query(CommonFile).filter(CommonFile.id == common_file_id)
    if lock:
        query = query.with_for_update()
    common_file = query.first()
    if common_file is None:
        raise ValueError("target CommonFile does not exist")
    if common_file.file_id.lower() != expected_sha256:
        raise ValueError("target CommonFile ID and SHA-256 do not match")
    if common_file.deleted:
        raise ValueError("target CommonFile is deleted")

    metadata_query = db.query(CommonFileMetadata).filter(
        CommonFileMetadata.file_id == common_file_id
    )
    if lock:
        metadata_query = metadata_query.with_for_update()
    metadata = metadata_query.first()
    if metadata is None:
        raise ValueError("target CommonFile has no metadata projection")
    return common_file, metadata


def _require_memorykeeper_link(db: Session, common_file_id: int) -> None:
    exists = (
        db.query(CommonFileService.id)
        .filter(CommonFileService.file_id == common_file_id)
        .filter(CommonFileService.service_name == "MemoryKeeper")
        .first()
    )
    if exists is None:
        raise ValueError("target CommonFile has no MemoryKeeper service link")


def _require_missing_gps(metadata: CommonFileMetadata) -> None:
    if metadata.gps_lat is not None or metadata.gps_lon is not None:
        raise ValueError("target GPS projection is not completely null")


def _reject_user_gps_history(db: Session, common_file_id: int) -> None:
    latest = HistoryRepository(db).get_latest_histories(
        file_id=common_file_id,
        field_names=["gps_lat", "gps_lon"],
    )
    if any(history.source == MetadataSource.USER for history in latest.values()):
        raise ValueError("latest USER GPS history requires manual review")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Repair missing GPS metadata for exactly one CommonFile",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Read only (default)")
    mode.add_argument("--execute", action="store_true", help="Persist the repair")
    parser.add_argument("--common-file-id", type=int, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument(
        "--match-place",
        action="store_true",
        help="Match an existing place after GPS repair; never creates a place",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.common_file_id < 1:
        raise SystemExit("--common-file-id must be at least 1")
    if re.fullmatch(r"[0-9a-fA-F]{64}", args.sha256) is None:
        raise SystemExit("--sha256 must be a 64-character hexadecimal SHA-256")

    db = SessionLocal()
    try:
        result = repair_missing_gps_metadata(
            db,
            storage_service=StorageService(),
            common_file_id=args.common_file_id,
            expected_sha256=args.sha256,
            execute=bool(args.execute),
            match_place=bool(args.match_place),
        )
    finally:
        db.close()

    print(" ".join(f"{key}={value}" for key, value in asdict(result).items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
