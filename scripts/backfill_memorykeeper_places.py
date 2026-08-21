"""Dry-run-first matching of existing MemoryKeeper files to registered places."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from app.common.database import SessionLocal
from app.memorykeeper.repositories.place_repository import MemoryKeeperPlaceRepository
from app.memorykeeper.services.place_matcher import MemoryKeeperPlaceMatcher
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService


@dataclass
class BackfillStats:
    scanned: int = 0
    matched: int = 0
    unchanged: int = 0
    unmatched: int = 0
    failed: int = 0


def backfill_memorykeeper_places(
    db: Session,
    *,
    execute: bool = False,
    limit: int | None = None,
) -> BackfillStats:
    stats = BackfillStats()
    repository = MemoryKeeperPlaceRepository(db)
    matcher = MemoryKeeperPlaceMatcher(db)
    service = MemoryKeeperPlaceService(db)
    rows = repository.memorykeeper_files_with_gps()
    if limit is not None:
        rows = rows[:limit]
    for common_file, metadata in rows:
        stats.scanned += 1
        try:
            match = matcher.match(
                gps_lat=float(metadata.gps_lat),
                gps_lon=float(metadata.gps_lon),
            )
            if not match.matched:
                stats.unmatched += 1
            elif metadata.memorykeeper_place_id == match.place.id:
                stats.unchanged += 1
            else:
                stats.matched += 1
                if execute:
                    service.auto_match_file(file_id=common_file.id)
        except Exception:
            if execute:
                db.rollback()
            stats.failed += 1
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill MemoryKeeper place relations")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report only (default)")
    mode.add_argument("--execute", action="store_true", help="Persist matched relations")
    parser.add_argument("--limit", type=int, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    db = SessionLocal()
    try:
        stats = backfill_memorykeeper_places(db, execute=bool(args.execute), limit=args.limit)
    finally:
        db.close()
    summary = asdict(stats)
    summary["mode"] = "execute" if args.execute else "dry-run"
    print(" ".join(f"{key}={value}" for key, value in summary.items()))
    return 0 if stats.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
