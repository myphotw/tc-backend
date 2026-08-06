"""운영 DB에 남은 성능/병렬 테스트 데이터 안전 정리.

기본은 dry-run. 실제 삭제는 --execute --confirm-backup 일 때만 수행한다.
"""

from __future__ import annotations

print("CLEANUP_IMPORT_START", flush=True)

import argparse
import json
import re
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_NAME_RE = re.compile(
    r"(?i)(qa_|phase2_|p3a_|perf_|warm\.jpg|dup1\.jpg|dup2\.jpg|"
    r"missing-phase|does-not-exist|qa-unique|qa_unique)"
)
TEST_JOB_PATH_RE = re.compile(
    r"(?i)(qa_|phase2|phase3|p3a_|perf_|warm|dup|sqlite-|"
    r"missing-phase|does-not-exist|qa-unique|qa_unique)"
)

print("CLEANUP_STDLIB_IMPORT_OK", flush=True)


@dataclass
class MediaInfo:
    original_path: str | None
    preview_path: str | None
    thumb_path: str | None
    original_exists: bool | None
    preview_exists: bool | None
    thumb_exists: bool | None
    original_abs: str | None = None
    preview_abs: str | None = None
    thumb_abs: str | None = None
    original_bytes: int = 0
    preview_bytes: int = 0
    thumb_bytes: int = 0
    outside_root: list[str] = field(default_factory=list)


@dataclass
class FileTarget:
    id: int
    file_id: str
    original_name: str
    reason: str
    media: MediaInfo


def parse_keep_ids(raw: str) -> set[int]:
    values = {int(part.strip()) for part in raw.split(",") if part.strip()}
    if not values:
        raise ValueError("keep-ids must not be empty")
    return values


def is_test_filename(name: str | None) -> bool:
    if not name:
        return False
    return TEST_NAME_RE.search(name) is not None


def is_test_job(job: Any) -> bool:
    path = (job.incoming_path or "")
    jid = (job.job_id or "")
    log = job.processing_log or ""
    if TEST_JOB_PATH_RE.search(path):
        return True
    if jid.lower().startswith(("qa-", "bad-", "sqlite-")):
        return True
    if any(
        marker in log
        for marker in (
            "UploadWorker-PERF",
            "UploadWorker-SQLite",
            "UploadWorker-PERF-RECOVER",
            "UploadWorker-PERF-DRAIN",
        )
    ):
        return True
    if ("UploadWorker-A" in log or "UploadWorker-B" in log) and TEST_JOB_PATH_RE.search(
        path
    ):
        return True
    return False


def build_media_info(storage: Any, root: Path, file_obj: Any) -> MediaInfo:
    outside: list[str] = []

    def inspect(rel: str | None) -> tuple[bool | None, str | None, int]:
        if not rel:
            return None, None, 0
        try:
            abs_path = storage.resolve_storage_path(rel).resolve()
        except Exception:
            return False, None, 0
        try:
            abs_path.relative_to(root.resolve())
        except ValueError:
            outside.append(str(abs_path))
            return False, str(abs_path), 0
        if not abs_path.is_file():
            return False, str(abs_path), 0
        return True, str(abs_path), abs_path.stat().st_size

    o_ex, o_abs, o_sz = inspect(file_obj.original_path)
    p_ex, p_abs, p_sz = inspect(file_obj.preview_path)
    t_ex, t_abs, t_sz = inspect(file_obj.thumb_path)
    return MediaInfo(
        original_path=file_obj.original_path,
        preview_path=file_obj.preview_path,
        thumb_path=file_obj.thumb_path,
        original_exists=o_ex,
        preview_exists=p_ex,
        thumb_exists=t_ex,
        original_abs=o_abs,
        preview_abs=p_abs,
        thumb_abs=t_abs,
        original_bytes=o_sz,
        preview_bytes=p_sz,
        thumb_bytes=t_sz,
        outside_root=outside,
    )


def verify_orphan_id1(file_obj: Any, keep_file_ids: set[str]) -> dict[str, Any]:
    media_missing = (
        not file_obj.original_path
        and not file_obj.preview_path
        and not file_obj.thumb_path
    )
    return {
        "id": file_obj.id,
        "file_id": file_obj.file_id,
        "original_name": file_obj.original_name,
        "file_id_collision_with_keep": file_obj.file_id in keep_file_ids,
        "paths_all_empty": media_missing,
    }


def safe_under_root(root: Path, abs_path: Path) -> bool:
    try:
        abs_path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def print_backup_help() -> None:
    print(
        """
=== PostgreSQL backup (required before --execute) ===
Example:
  pg_dump -h <HOST> -p <PORT> -U <USER> -d <DB> -F c -f tc_backend_backup_YYYYMMDD.dump

Or plain SQL:
  pg_dump -h <HOST> -p <PORT> -U <USER> -d <DB> > tc_backend_backup_YYYYMMDD.sql

After backup completes, re-run with:
  python scripts/cleanup_perf_test_data.py --execute --confirm-backup --keep-ids 2,3,4
""".strip(),
        flush=True,
    )


def _open_db_session():
    """스크립트 전용 engine (connect/statement/lock timeout). import 시 DB 접속 안 함."""
    print("IMPORT_SETTINGS_START", flush=True)
    from app.common.config import settings

    print("IMPORT_SETTINGS_OK", flush=True)

    print("IMPORT_DATABASE_MODULE_START", flush=True)
    # database 모듈 import는 create_engine만 하고 즉시 connect 하지는 않음.
    # 모델 등록을 위해 import는 필요하지만, Session은 timeout engine으로 연다.
    from app.common import database as database_module  # noqa: F401
    from app.common.models.file import CommonFile
    from app.common.models.file_metadata import CommonFileMetadata
    from app.common.models.file_tag import CommonFileTag
    from app.common.models.metadata_history import CommonMetadataHistory
    from app.common.models.upload_job import UploadJob
    from app.common.models.vision_job import CommonVisionJob

    print("IMPORT_DATABASE_MODULE_OK", flush=True)

    print("IMPORT_STORAGE_START", flush=True)
    from app.common.services.storage_service import StorageService

    print("IMPORT_STORAGE_OK", flush=True)

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    database_url = (
        f"postgresql://"
        f"{settings.POSTGRES_USER}:"
        f"{quote_plus(settings.POSTGRES_PASSWORD)}@"
        f"{settings.POSTGRES_HOST}:"
        f"{settings.POSTGRES_PORT}/"
        f"{settings.POSTGRES_DB}"
    )
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=10,
        connect_args={
            "connect_timeout": 10,
            "options": "-c statement_timeout=60000 -c lock_timeout=10000",
        },
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return (
        settings,
        StorageService,
        Session,
        engine,
        text,
        CommonFile,
        CommonFileMetadata,
        CommonFileTag,
        CommonMetadataHistory,
        UploadJob,
        CommonVisionJob,
    )


def main(argv: list[str] | None = None) -> int:
    print("SCRIPT_START", flush=True)
    parser = argparse.ArgumentParser(description="Cleanup perf/parallel test data safely")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Plan only (default)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete DB rows and storage files",
    )
    parser.add_argument(
        "--confirm-backup",
        action="store_true",
        help="Required with --execute to confirm pg_dump/backup is done",
    )
    parser.add_argument("--keep-ids", default="2,3,4", help="Comma-separated keep ids")
    parser.add_argument(
        "--backup-report",
        default="",
        help="JSON report output path (default scripts/cleanup_perf_test_data_report.json)",
    )
    args = parser.parse_args(argv)
    print("ARGS_PARSED", vars(args), flush=True)

    do_execute = bool(args.execute)
    if do_execute and not args.confirm_backup:
        print_backup_help()
        print("REFUSED: --execute requires --confirm-backup", flush=True)
        return 2

    keep_ids = parse_keep_ids(args.keep_ids)
    report_path = Path(
        args.backup_report
        or (ROOT / "scripts" / "cleanup_perf_test_data_report.json")
    )

    (
        settings,
        StorageService,
        Session,
        engine,
        text,
        CommonFile,
        CommonFileMetadata,
        CommonFileTag,
        CommonMetadataHistory,
        UploadJob,
        CommonVisionJob,
    ) = _open_db_session()

    print("STORAGE_ROOT_RESOLVE_START", flush=True)
    storage = StorageService()
    root = Path(settings.PHOTO_PLATFORM_ROOT)
    # resolve() can hang on dead NAS mounts; keep absolute without forcing network resolve first.
    try:
        root = root.expanduser().absolute()
    except Exception as exc:
        print(f"STORAGE_ROOT_RESOLVE_WARN {exc}", flush=True)
    print(f"STORAGE_ROOT_RESOLVE_OK root={root}", flush=True)

    print("DB_CONNECT_START", flush=True)
    db = Session()
    try:
        db.execute(text("SELECT 1"))
        print("DB_CONNECT_OK", flush=True)

        print("TARGET_QUERY_START", flush=True)
        active = (
            db.query(CommonFile)
            .filter(CommonFile.deleted.is_(False))
            .order_by(CommonFile.id.asc())
            .all()
        )
        print(f"TARGET_QUERY_OK active_files={len(active)}", flush=True)

        keep_files = [f for f in active if f.id in keep_ids]
        missing_keep = sorted(keep_ids - {f.id for f in keep_files})
        if missing_keep:
            raise RuntimeError(f"keep-ids not found among active files: {missing_keep}")

        keep_file_ids = {f.file_id for f in keep_files}
        keep_paths = set()
        for f in keep_files:
            for rel in (f.original_path, f.preview_path, f.thumb_path):
                if rel:
                    keep_paths.add(rel.replace("\\", "/"))

        orphan_id1_report: dict[str, Any] | None = None
        id1 = next((f for f in active if f.id == 1), None)
        if id1 is not None and 1 not in keep_ids:
            print("ID1_VALIDATE_START", flush=True)
            media = build_media_info(storage, root, id1)
            orphan_id1_report = verify_orphan_id1(id1, keep_file_ids)
            orphan_id1_report.update(
                {
                    "original_exists": media.original_exists,
                    "preview_exists": media.preview_exists,
                    "thumb_exists": media.thumb_exists,
                    "outside_root": media.outside_root,
                }
            )
            files_absent = (
                media.original_exists is not True
                and media.preview_exists is not True
                and media.thumb_exists is not True
            )
            print(f"ID1_VALIDATE_OK absent={files_absent}", flush=True)
            if orphan_id1_report["file_id_collision_with_keep"] or not files_absent:
                report = {
                    "mode": "aborted",
                    "reason": "id=1 orphan validation failed",
                    "id1": orphan_id1_report,
                }
                report_path.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
                print("ABORTED: id=1 validation failed; no changes made", flush=True)
                return 3

        print("UPLOAD_JOB_QUERY_START", flush=True)
        jobs = db.query(UploadJob).all()
        print(f"UPLOAD_JOB_QUERY_OK count={len(jobs)}", flush=True)
        test_jobs = [j for j in jobs if is_test_job(j)]
        test_file_ids_from_jobs = {j.file_id for j in test_jobs if j.file_id}

        delete_targets: list[FileTarget] = []
        candidates = [f for f in active if f.id not in keep_ids]
        print(f"MEDIA_SCAN_START count={len(candidates)}", flush=True)
        for idx, f in enumerate(candidates, start=1):
            if idx == 1 or idx % 25 == 0 or idx == len(candidates):
                print(
                    f"MEDIA_SCAN_PROGRESS {idx}/{len(candidates)} id={f.id}",
                    flush=True,
                )
            media = build_media_info(storage, root, f)
            reasons: list[str] = []
            if f.id == 1:
                reasons.append("orphan_id_1_no_media_files")
            if is_test_filename(f.original_name):
                reasons.append("test_filename")
            if f.file_id in test_file_ids_from_jobs:
                reasons.append("linked_test_upload_job")
            if not reasons:
                continue
            delete_targets.append(
                FileTarget(
                    id=f.id,
                    file_id=f.file_id,
                    original_name=f.original_name,
                    reason="+".join(reasons),
                    media=media,
                )
            )
        print(f"MEDIA_SCAN_OK delete_targets={len(delete_targets)}", flush=True)

        delete_pks = [t.id for t in delete_targets]
        delete_file_ids = {t.file_id for t in delete_targets}

        jobs_to_delete = []
        for j in jobs:
            if j.file_id and j.file_id in {f.file_id for f in keep_files}:
                continue
            if is_test_job(j) or (j.file_id and j.file_id in delete_file_ids):
                jobs_to_delete.append(j)

        print("RELATED_COUNT_START", flush=True)
        meta_n = (
            db.query(CommonFileMetadata)
            .filter(CommonFileMetadata.file_id.in_(delete_pks))
            .count()
            if delete_pks
            else 0
        )
        tag_n = (
            db.query(CommonFileTag)
            .filter(CommonFileTag.file_id.in_(delete_pks))
            .count()
            if delete_pks
            else 0
        )
        hist_n = (
            db.query(CommonMetadataHistory)
            .filter(CommonMetadataHistory.file_id.in_(delete_pks))
            .count()
            if delete_pks
            else 0
        )
        vision_n = (
            db.query(CommonVisionJob)
            .filter(CommonVisionJob.file_id.in_(delete_pks))
            .count()
            if delete_pks
            else 0
        )
        print("RELATED_COUNT_OK", flush=True)

        storage_candidates: list[dict[str, Any]] = []
        total_bytes = 0
        outside_root_paths: list[str] = []
        for t in delete_targets:
            for kind, rel, exists, abs_path, nbytes in (
                (
                    "original",
                    t.media.original_path,
                    t.media.original_exists,
                    t.media.original_abs,
                    t.media.original_bytes,
                ),
                (
                    "preview",
                    t.media.preview_path,
                    t.media.preview_exists,
                    t.media.preview_abs,
                    t.media.preview_bytes,
                ),
                (
                    "thumb",
                    t.media.thumb_path,
                    t.media.thumb_exists,
                    t.media.thumb_abs,
                    t.media.thumb_bytes,
                ),
            ):
                if not rel:
                    continue
                norm = rel.replace("\\", "/")
                if norm in keep_paths:
                    continue
                if t.media.outside_root:
                    outside_root_paths.extend(t.media.outside_root)
                storage_candidates.append(
                    {
                        "file_pk": t.id,
                        "kind": kind,
                        "relative": rel,
                        "absolute": abs_path,
                        "exists": exists,
                        "bytes": nbytes,
                    }
                )
                if exists is True:
                    total_bytes += nbytes

        skipped_active = [
            {
                "id": f.id,
                "file_id": f.file_id,
                "original_name": f.original_name,
            }
            for f in active
            if f.id not in keep_ids and f.id not in set(delete_pks)
        ]

        print("KEEP_MEDIA_REFRESH_START", flush=True)
        keep_files_payload = []
        for f in keep_files:
            keep_files_payload.append(
                {
                    "id": f.id,
                    "file_id": f.file_id,
                    "original_name": f.original_name,
                    "media": asdict(build_media_info(storage, root, f)),
                }
            )
        print("KEEP_MEDIA_REFRESH_OK", flush=True)

        report: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "execute" if do_execute else "dry-run",
            "storage_root": str(root),
            "keep_ids": sorted(keep_ids),
            "keep_files": keep_files_payload,
            "id1_orphan_validation": orphan_id1_report,
            "delete_common_files_count": len(delete_targets),
            "delete_common_files_sample_20": [
                {
                    "id": t.id,
                    "file_id": t.file_id,
                    "original_name": t.original_name,
                    "reason": t.reason,
                    "media": asdict(t.media),
                }
                for t in delete_targets[:20]
            ],
            "delete_common_files_ids": delete_pks,
            "related_counts": {
                "metadata": meta_n,
                "tags": tag_n,
                "history": hist_n,
                "vision_jobs": vision_n,
                "upload_jobs": len(jobs_to_delete),
            },
            "storage_plan": {
                "candidate_paths": len(storage_candidates),
                "existing_files_to_delete": sum(
                    1 for c in storage_candidates if c["exists"] is True
                ),
                "missing_files": sum(
                    1 for c in storage_candidates if c["exists"] is False
                ),
                "expected_bytes": total_bytes,
                "expected_mb": round(total_bytes / (1024 * 1024), 2),
                "outside_root_paths": sorted(set(outside_root_paths)),
            },
            "skipped_non_matching_active_files": skipped_active,
            "backup_help": (
                "pg_dump -h <HOST> -p <PORT> -U <USER> -d <DB> -F c "
                "-f tc_backend_backup_YYYYMMDD.dump"
            ),
            "execute_command": (
                "python scripts/cleanup_perf_test_data.py --execute "
                "--confirm-backup --keep-ids 2,3,4"
            ),
        }

        if do_execute and outside_root_paths:
            report["mode"] = "aborted"
            report["reason"] = "storage path outside PHOTO_PLATFORM_ROOT"
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            print("ABORTED: refusing to delete paths outside storage root", flush=True)
            return 4

        if not do_execute:
            print("DRY_RUN_WRITE_REPORT_START", flush=True)
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print("DRY_RUN_WRITE_REPORT_OK", flush=True)
            print(
                json.dumps(
                    {
                        "mode": report["mode"],
                        "keep_ids": report["keep_ids"],
                        "delete_common_files_count": report["delete_common_files_count"],
                        "related_counts": report["related_counts"],
                        "storage_plan": report["storage_plan"],
                        "id1_orphan_validation": report["id1_orphan_validation"],
                        "sample_20_ids": [
                            x["id"] for x in report["delete_common_files_sample_20"]
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                flush=True,
            )
            print(f"DRY_RUN_REPORT={report_path}", flush=True)
            print_backup_help()
            print("No DB/storage changes were made.", flush=True)
            print("DRY_RUN_COMPLETE", flush=True)
            return 0

        # ---------------- execute ----------------
        print("EXECUTE_DB_DELETE_START", flush=True)
        storage_results: list[dict[str, Any]] = []
        try:
            pks = list(delete_pks)
            if pks:
                db.query(CommonMetadataHistory).filter(
                    CommonMetadataHistory.file_id.in_(pks)
                ).delete(synchronize_session=False)
                db.query(CommonFileTag).filter(
                    CommonFileTag.file_id.in_(pks)
                ).delete(synchronize_session=False)
                db.query(CommonFileMetadata).filter(
                    CommonFileMetadata.file_id.in_(pks)
                ).delete(synchronize_session=False)
                db.query(CommonVisionJob).filter(
                    CommonVisionJob.file_id.in_(pks)
                ).delete(synchronize_session=False)

            job_ids = [j.id for j in jobs_to_delete]
            if job_ids:
                db.query(UploadJob).filter(UploadJob.id.in_(job_ids)).delete(
                    synchronize_session=False
                )

            if pks:
                db.query(CommonFile).filter(CommonFile.id.in_(pks)).delete(
                    synchronize_session=False
                )

            db.commit()
            print("EXECUTE_DB_COMMIT_OK", flush=True)
        except Exception as exc:
            db.rollback()
            report["mode"] = "execute_failed_rolled_back"
            report["error"] = str(exc)
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            print("EXECUTE FAILED: DB rolled back; storage untouched", flush=True)
            return 5

        print(f"EXECUTE_STORAGE_DELETE_START candidates={len(storage_candidates)}", flush=True)
        for i, candidate in enumerate(storage_candidates, start=1):
            if i == 1 or i % 50 == 0 or i == len(storage_candidates):
                print(f"EXECUTE_STORAGE_PROGRESS {i}/{len(storage_candidates)}", flush=True)
            abs_str = candidate.get("absolute")
            rel = candidate.get("relative")
            result = {
                "relative": rel,
                "absolute": abs_str,
                "kind": candidate["kind"],
                "file_pk": candidate["file_pk"],
                "status": "skipped",
            }
            if not abs_str:
                result["status"] = "missing_path"
                storage_results.append(result)
                continue
            abs_path = Path(abs_str)
            if not safe_under_root(root, abs_path):
                result["status"] = "blocked_outside_root"
                storage_results.append(result)
                continue
            if rel and rel.replace("\\", "/") in keep_paths:
                result["status"] = "blocked_keep_path"
                storage_results.append(result)
                continue
            try:
                if abs_path.is_file():
                    abs_path.unlink()
                    result["status"] = "deleted"
                else:
                    result["status"] = "already_absent"
            except Exception as exc:
                result["status"] = f"error:{exc}"
            storage_results.append(result)
        print("EXECUTE_STORAGE_DELETE_OK", flush=True)

        remaining = (
            db.query(CommonFile)
            .filter(CommonFile.deleted.is_(False))
            .order_by(CommonFile.id.asc())
            .all()
        )
        remaining_test_jobs = [j for j in db.query(UploadJob).all() if is_test_job(j)]
        remaining_vision_for_deleted = 0
        if delete_pks:
            remaining_vision_for_deleted = (
                db.query(CommonVisionJob)
                .filter(CommonVisionJob.file_id.in_(delete_pks))
                .count()
            )

        report["execution"] = {
            "db_committed": True,
            "storage_results_summary": {
                "deleted": sum(1 for r in storage_results if r["status"] == "deleted"),
                "already_absent": sum(
                    1 for r in storage_results if r["status"] == "already_absent"
                ),
                "errors": [
                    r for r in storage_results if str(r["status"]).startswith("error:")
                ],
                "blocked": [
                    r
                    for r in storage_results
                    if str(r["status"]).startswith("blocked_")
                ],
            },
            "post_verify": {
                "active_common_files": len(remaining),
                "remaining_ids": [f.id for f in remaining],
                "expected_ids": sorted(keep_ids),
                "match_keep_ids": [f.id for f in remaining] == sorted(keep_ids),
                "remaining_test_upload_jobs": len(remaining_test_jobs),
                "remaining_vision_for_deleted_pks": remaining_vision_for_deleted,
            },
        }
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(report["execution"], ensure_ascii=False, indent=2),
            flush=True,
        )
        print(f"EXECUTE_REPORT={report_path}", flush=True)
        print("EXECUTE_COMPLETE", flush=True)
        return 0 if report["execution"]["post_verify"]["match_keep_ids"] else 6
    finally:
        db.close()
        engine.dispose()
        print("DB_SESSION_CLOSED", flush=True)


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception:
        traceback.print_exc()
        raise
    raise SystemExit(exit_code)
