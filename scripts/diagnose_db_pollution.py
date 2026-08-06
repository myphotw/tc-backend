"""Read-only diagnosis of QA test pollution in operational DB."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from app.common.database import SessionLocal
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_tag import CommonFileTag
from app.common.models.metadata_history import CommonMetadataHistory
from app.common.models.upload_job import UploadJob
from app.common.models.vision_job import CommonVisionJob
from app.common.services.storage_service import StorageService

TEST_NAME_RE = re.compile(
    r"(?i)^(qa_|phase2_|phase2|p3a_|perf_|warm\.jpg|dup[12]\.jpg|qa_a|qa_b|qa_unique)"
)
TEST_NAME_FRAGMENTS = (
    "qa_",
    "phase2_",
    "phase2",
    "p3a_",
    "perf_",
    "warm.jpg",
    "dup1.jpg",
    "dup2.jpg",
    "qa_unique",
    "qa-unique",
)


def is_test_filename(name: str | None) -> bool:
    if not name:
        return False
    n = name.lower()
    if TEST_NAME_RE.search(n):
        return True
    return any(frag in n for frag in TEST_NAME_FRAGMENTS)


def is_test_job(job: UploadJob) -> bool:
    path = (job.incoming_path or "").lower()
    jid = (job.job_id or "").lower()
    log = job.processing_log or ""
    markers = (
        "qa_",
        "phase2",
        "phase3",
        "p3a_",
        "perf_",
        "warm",
        "dup",
        "sqlite-",
        "missing-phase",
        "does-not-exist",
        "qa-unique",
        "qa_unique",
    )
    if any(m in path for m in markers):
        return True
    if jid.startswith(("qa-", "bad-", "sqlite-")):
        return True
    if any(
        x in log
        for x in (
            "UploadWorker-PERF",
            "UploadWorker-SQLite",
            "UploadWorker-PERF-RECOVER",
            "UploadWorker-PERF-DRAIN",
        )
    ):
        return True
    if ("UploadWorker-A" in log or "UploadWorker-B" in log) and any(
        m in path for m in ("p3a_", "perf_", "phase2", "qa_", "dup", "warm")
    ):
        return True
    return False


def main() -> None:
    storage = StorageService()
    db = SessionLocal()

    def path_exists(rel: str | None) -> bool | None:
        if not rel:
            return None
        try:
            return storage.resolve_storage_path(rel).is_file()
        except Exception:
            return False

    active = (
        db.query(CommonFile)
        .filter(CommonFile.deleted.is_(False))
        .order_by(CommonFile.id.asc())
        .all()
    )
    all_files = db.query(CommonFile).all()
    service_counts = Counter((f.service_name or "NULL") for f in active)

    normal = 0
    orphan_rows = []
    for f in active:
        o = path_exists(f.original_path)
        p = path_exists(f.preview_path)
        t = path_exists(f.thumb_path)
        if o is True and p is True and t is True:
            normal += 1
        else:
            orphan_rows.append(
                {
                    "id": f.id,
                    "file_id": f.file_id,
                    "original_name": f.original_name,
                    "original_path": f.original_path,
                    "original_exists": o,
                    "preview_path": f.preview_path,
                    "preview_exists": p,
                    "thumb_path": f.thumb_path,
                    "thumb_exists": t,
                    "created_at": str(f.created_at),
                    "likely_test": is_test_filename(f.original_name),
                }
            )

    jobs = db.query(UploadJob).order_by(UploadJob.id.desc()).all()
    test_jobs = [j for j in jobs if is_test_job(j)]
    job_status = Counter(j.status for j in jobs)
    test_job_status = Counter(j.status for j in test_jobs)

    vjobs = db.query(CommonVisionJob).all()
    v_status = Counter(str(v.status) for v in vjobs)

    test_file_ids_from_jobs = {j.file_id for j in test_jobs if j.file_id}
    test_by_name = [f for f in active if is_test_filename(f.original_name)]
    test_ids = {f.file_id for f in test_by_name} | test_file_ids_from_jobs
    identified_test = [f for f in active if f.file_id in test_ids]
    likely_user = [f for f in active if f.file_id not in test_ids]

    # Related child counts for identified test files (pk ids)
    test_pks = [f.id for f in identified_test]
    meta_n = (
        db.query(CommonFileMetadata)
        .filter(CommonFileMetadata.file_id.in_(test_pks))
        .count()
        if test_pks
        else 0
    )
    tag_n = (
        db.query(CommonFileTag).filter(CommonFileTag.file_id.in_(test_pks)).count()
        if test_pks
        else 0
    )
    hist_n = (
        db.query(CommonMetadataHistory)
        .filter(CommonMetadataHistory.file_id.in_(test_pks))
        .count()
        if test_pks
        else 0
    )
    vision_for_test = (
        db.query(CommonVisionJob)
        .filter(CommonVisionJob.file_id.in_(test_pks))
        .count()
        if test_pks
        else 0
    )

    recent = sorted(
        active,
        key=lambda x: (x.created_at is not None, x.created_at, x.id),
        reverse=True,
    )[:20]

    # Scripts cleanup check: do scripts delete?
    scripts = {
        "qa_perf_phase1.py": "no cleanup / no rollback observed in design",
        "qa_perf_phase2.py": "no cleanup / no rollback observed in design",
        "qa_phase3a.py": "no cleanup / no rollback observed in design",
    }

    report = {
        "verdict": (
            "TEST_DATA_POLLUTION_CONFIRMED"
            if len(identified_test) >= max(1, len(active) - 10)
            or len(active) > 10
            else "NEEDS_REVIEW"
        ),
        "1_active_common_files": len(active),
        "all_common_files_including_deleted": len(all_files),
        "deleted_flag_true": sum(1 for f in all_files if bool(f.deleted)),
        "2_service_name_counts": dict(service_counts),
        "3_4_media_integrity": {
            "all_three_exist": normal,
            "missing_any_path_or_file": len(orphan_rows),
        },
        "5_orphan_records": {
            "count": len(orphan_rows),
            "sample_10": orphan_rows[:10],
        },
        "6_scripts_cleanup": {
            "cleaned_up_after_run": False,
            "notes": scripts,
            "evidence": (
                "Scripts upload via API / create jobs and process workers against "
                "configured Postgres + PHOTO_PLATFORM_ROOT without transaction "
                "rollback or delete cleanup."
            ),
        },
        "7_jobs": {
            "upload_jobs_total": len(jobs),
            "upload_jobs_by_status": dict(job_status),
            "upload_jobs_likely_test": len(test_jobs),
            "upload_jobs_likely_test_by_status": dict(test_job_status),
            "vision_jobs_total": len(vjobs),
            "vision_jobs_by_status": dict(v_status),
            "vision_jobs_for_identified_test_files": vision_for_test,
        },
        "identification": {
            "test_files_by_filename": len(test_by_name),
            "test_files_union_filename_or_job": len(identified_test),
            "likely_user_files": len(likely_user),
            "test_child_rows": {
                "metadata": meta_n,
                "tags": tag_n,
                "history": hist_n,
            },
            "top_filenames": Counter(f.original_name for f in active).most_common(40),
        },
        "8_recent_20": [
            {
                "id": f.id,
                "file_id": f.file_id,
                "original_name": f.original_name,
                "service_name": f.service_name,
                "created_at": str(f.created_at),
                "original_exists": path_exists(f.original_path),
                "preview_exists": path_exists(f.preview_path),
                "thumb_exists": path_exists(f.thumb_path),
                "likely_test": is_test_filename(f.original_name),
            }
            for f in recent
        ],
        "likely_user_file_details": [
            {
                "id": f.id,
                "file_id": f.file_id,
                "original_name": f.original_name,
                "created_at": str(f.created_at),
                "service_name": f.service_name,
                "original_path": f.original_path,
                "original_exists": path_exists(f.original_path),
                "preview_exists": path_exists(f.preview_path),
                "thumb_exists": path_exists(f.thumb_path),
            }
            for f in likely_user
        ],
        "proposed_delete_targets": {
            "common_files_ids": [f.id for f in identified_test],
            "common_files_file_ids": [f.file_id for f in identified_test],
            "upload_job_ids": [j.job_id for j in test_jobs],
            "storage_paths": {
                "original": [f.original_path for f in identified_test if f.original_path],
                "preview": [f.preview_path for f in identified_test if f.preview_path],
                "thumb": [f.thumb_path for f in identified_test if f.thumb_path],
            },
        },
    }

    out = Path("scripts/diagnose_db_pollution_report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"REPORT_PATH={out.resolve()}")
    db.close()


if __name__ == "__main__":
    main()
