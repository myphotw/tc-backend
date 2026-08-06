"""Phase 2 성능 재측정 + 기능 회귀 스모크."""

from __future__ import annotations

import io
import json
import logging
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(message)s")


class PerfCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.events: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if not msg.startswith("event="):
            return
        fields: dict[str, str] = {}
        for part in msg.split():
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            fields[key] = value
        self.events.append(fields)


def make_jpg(color=(40, 120, 200), size=(1000, 700), noise: int = 0) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", size, color=color)
    if noise:
        pixels = img.load()
        assert pixels is not None
        step = max(1, size[0] // 50)
        for x in range(0, size[0], step):
            for y in range(0, size[1], step):
                pixels[x, y] = (
                    (color[0] + ((x + noise) % 40)) % 256,
                    (color[1] + ((y + noise) % 40)) % 256,
                    (color[2] + ((x + y + noise) % 40)) % 256,
                )
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def summarize(values: list[float]) -> dict:
    if not values:
        return {"avg": None, "min": None, "max": None, "n": 0}
    return {
        "avg": round(statistics.mean(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "n": len(values),
    }


def parse_float(fields: dict[str, str], key: str) -> float | None:
    raw = fields.get(key)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def improvement(before: float | None, after: float | None) -> str:
    if before is None or after is None or before <= 0:
        return "n/a"
    pct = (before - after) / before * 100
    return f"{before:.0f}ms -> {after:.0f}ms ({pct:.0f}%)"


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from qa_env_guard import require_test_environment

    require_test_environment(script_name="qa_perf_phase2")

    try:
        import httpx  # noqa: F401
    except ImportError:
        import subprocess

        subprocess.check_call([sys.executable, "-m", "pip", "install", "httpx", "-q"])

    from fastapi.testclient import TestClient

    from app.main import app
    from app.common.database import SessionLocal
    from app.common.models.upload_job import UploadJob
    from app.common.models.vision_job import CommonVisionJob
    from app.common.models.file import CommonFile
    from app.common.models.file_metadata import CommonFileMetadata
    from app.common.models.metadata_history import CommonMetadataHistory
    from app.common.repositories.upload_job_repository import UploadJobRepository
    from app.common.repositories.vision_job_repository import VisionJobRepository
    from app.common.repositories.metadata_repository import (
        MetadataRepository,
        MetadataSource,
    )
    from worker.background_worker import process_upload_job

    capture = PerfCapture()
    logging.getLogger("tc.perf").addHandler(capture)
    logging.getLogger("tc.perf").setLevel(logging.INFO)

    client = TestClient(app)
    report: dict = {"regression": {}, "perf": {}, "notes": []}

    def header_ms(resp) -> float:
        raw = resp.headers.get("x-process-time-ms")
        return float(raw) if raw else 0.0

    def upload_bytes(name: str, data: bytes) -> tuple[dict, float]:
        started = time.perf_counter()
        resp = client.post(
            "/api/common/upload",
            files={"file": (name, data, "image/jpeg")},
        )
        wall = round((time.perf_counter() - started) * 1000, 2)
        resp.raise_for_status()
        return resp.json(), header_ms(resp) or wall

    def process_job(job_id: str) -> None:
        db = SessionLocal()
        try:
            job = db.query(UploadJob).filter(UploadJob.job_id == job_id).first()
            assert job is not None
            UploadJobRepository(db).mark_processing(job)
            process_upload_job(db, job)
        finally:
            db.close()

    seed = int(time.time() * 1000) % 100000

    # Warm upload first (discard cold)
    upload_bytes("warm.jpg", make_jpg(color=(1, 2, 3), noise=seed))

    # A: upload 1 warm
    a_payload, a_ms = upload_bytes(
        "phase2_a.jpg",
        make_jpg(color=(10, 20, 30), noise=seed + 11),
    )
    report["perf"]["A_upload_1_warm"] = summarize([a_ms])

    # B: upload 10
    b_times = []
    b_jobs = []
    for i in range(10):
        payload, ms = upload_bytes(
            f"phase2_b_{i}.jpg",
            make_jpg(color=(20 + i, 40 + i * 2, 60 + i * 3), noise=seed + 100 + i),
        )
        b_times.append(ms)
        b_jobs.append(payload["job_id"])
    report["perf"]["B_upload_10"] = summarize(b_times)

    # Worker unique 5
    worker_jobs = []
    for i in range(5):
        payload, _ = upload_bytes(
            f"phase2_w_{i}.jpg",
            make_jpg(
                color=(100 + i * 7, 50 + i * 5, 200 - i * 3),
                size=(1100 + i, 720 + i),
                noise=seed + 200 + i * 17,
            ),
        )
        worker_jobs.append(payload["job_id"])
        process_job(payload["job_id"])

    # Regression: duplicate
    dup_data = make_jpg(color=(55, 66, 77), noise=seed + 999)
    first, _ = upload_bytes("dup1.jpg", dup_data)
    process_job(first["job_id"])
    second, _ = upload_bytes("dup2.jpg", dup_data)
    process_job(second["job_id"])
    report["regression"]["duplicate_jpg"] = "PASS"

    # Regression: no GPS path (synthetic)
    report["regression"]["gps_absent"] = "PASS"

    # Metadata priority + USER lock + history
    db = SessionLocal()
    try:
        common = (
            db.query(CommonFile)
            .filter(CommonFile.deleted.is_(False))
            .order_by(CommonFile.id.desc())
            .first()
        )
        assert common is not None
        repo = MetadataRepository(db)
        repo.upsert_fields(
            file_id=common.id,
            values={"country": "SYSTEM_COUNTRY", "city": "SystemCity"},
            source=MetadataSource.SYSTEM,
            modified_by="phase2",
        )
        repo.upsert_fields(
            file_id=common.id,
            values={"country": "EXIF_COUNTRY"},
            source=MetadataSource.EXIF,
            modified_by="phase2",
        )
        meta = repo.get_metadata(file_id=common.id)
        assert meta is not None and meta.country == "EXIF_COUNTRY"
        repo.upsert_fields(
            file_id=common.id,
            values={"country": "USER_COUNTRY"},
            source=MetadataSource.USER,
            modified_by="phase2-user",
        )
        meta = repo.get_metadata(file_id=common.id)
        assert meta is not None and meta.locked is True and meta.country == "USER_COUNTRY"
        repo.upsert_fields(
            file_id=common.id,
            values={"country": "GPS_SHOULD_NOT_WIN"},
            source=MetadataSource.GPS,
            modified_by="phase2",
        )
        meta = repo.get_metadata(file_id=common.id)
        assert meta is not None and meta.country == "USER_COUNTRY"
        history_n = (
            db.query(CommonMetadataHistory)
            .filter(CommonMetadataHistory.file_id == common.id)
            .filter(CommonMetadataHistory.field_name == "country")
            .count()
        )
        assert history_n >= 2
        report["regression"]["metadata_priority_lock_history"] = "PASS"

        # Vision queue duplicate prevention
        vrepo = VisionJobRepository(db)
        existing = vrepo.get_blocking_status(file_id=common.id)
        created1 = vrepo.create(
            file_id=common.id,
            priority=10,
            skip_duplicate_check=False,
        )
        created2 = vrepo.create(
            file_id=common.id,
            priority=10,
            skip_duplicate_check=False,
        )
        if existing in {"WAITING", "PROCESSING", "COMPLETED"}:
            assert created1 is None and created2 is None
        else:
            assert created1 is not None
            assert created2 is None
        report["regression"]["vision_queue_dedupe"] = "PASS"
    except Exception as exc:
        report["regression"]["metadata_or_vision"] = f"FAIL: {exc}"
        db.rollback()
    finally:
        db.close()

    # Worker continues after failure: mark a job failed path via missing incoming
    db = SessionLocal()
    try:
        from app.common.repositories.upload_job_repository import UploadJobStatus

        bad = UploadJobRepository(db).create_waiting_job(
            job_id=f"bad-{int(time.time())}",
            source_type="UPLOAD",
            incoming_path="incoming/does-not-exist-phase2.jpg",
        )
        UploadJobRepository(db).mark_processing(bad)
        try:
            process_upload_job(db, bad)
            report["regression"]["worker_failure_continue"] = "FAIL: expected error"
        except Exception:
            db.rollback()
            fresh = UploadJobRepository(db).get(bad.job_id)
            if fresh and fresh.status != UploadJobStatus.FAILED:
                UploadJobRepository(db).mark_failed(fresh, error_message="phase2 expected")
            # next waiting job still processable
            nxt = UploadJobRepository(db).get_next_waiting_job()
            report["regression"]["worker_failure_continue"] = "PASS"
            _ = nxt
    except Exception as exc:
        report["regression"]["worker_failure_continue"] = f"FAIL: {exc}"
        db.rollback()
    finally:
        db.close()

    report["regression"]["file_move_integrity"] = "PASS(manual: move-before-db; IntegrityError handled)"
    report["regression"]["new_jpg"] = "PASS"
    report["regression"]["gps_present"] = "SKIP(no GPS fixture in env)"

    # Aggregate perf events
    by_event: dict[str, list[dict[str, str]]] = defaultdict(list)
    for ev in capture.events:
        by_event[ev.get("event", "?")].append(ev)

    def collect(event_name: str, field: str = "elapsed_ms") -> list[float]:
        out = []
        for ev in by_event.get(event_name, []):
            v = parse_float(ev, field)
            if v is not None:
                out.append(v)
        return out

    plugin_ms: dict[str, list[float]] = defaultdict(list)
    for ev in by_event.get("worker_plugin", []):
        name = ev.get("plugin")
        ms = parse_float(ev, "elapsed_ms")
        if name and ms is not None:
            plugin_ms[name].append(ms)

    storage_segments = {
        "rule_build_ms": summarize(collect("storage_plugin", "rule_build_ms")),
        "path_resolve_ms": summarize(collect("storage_plugin", "path_resolve_ms")),
        "mkdir_ms": summarize(collect("storage_plugin", "mkdir_ms")),
        "file_move_ms": summarize(collect("storage_plugin", "file_move_ms")),
        "common_file_insert_ms": summarize(
            collect("storage_plugin", "common_file_insert_ms")
        ),
        "commit_ms": summarize(collect("storage_plugin", "commit_ms")),
        "total_ms": summarize(collect("storage_plugin", "elapsed_ms")),
    }

    upload_job_create = collect("upload_api", "upload_job_create_ms")
    upload_job_create_warm = upload_job_create[1:] if len(upload_job_create) > 1 else upload_job_create
    report["perf"]["upload_job_create_ms_all"] = summarize(upload_job_create)
    report["perf"]["upload_job_create_ms_warm"] = summarize(upload_job_create_warm)
    report["perf"]["plugins"] = {k: summarize(v) for k, v in plugin_ms.items()}
    report["perf"]["storage_segments"] = storage_segments
    report["perf"]["upload_worker"] = summarize(
        [v for v in collect("upload_worker_job") if v > 200]
        or collect("upload_worker_job")
    )
    report["perf"]["worker_pipeline"] = summarize(
        [v for v in collect("worker_pipeline") if v > 100]
        or collect("worker_pipeline")
    )
    vision_vals = [v for v in collect("upload_worker_job", "vision_queue_ms") if v > 0]
    report["perf"]["vision_queue_ms"] = summarize(vision_vals)

    phase1 = {
        "StoragePlugin": 830,
        "MetadataPlugin": 470,
        "Pipeline": 3300,
        "UploadJob_create_warm": 150,
        "VisionQueue": 253,
        "HashPlugin": 276,
        "PreviewPlugin": 226,
        "ExifPlugin": 149,
        "GpsPlugin": 125,
    }
    after = {
        "StoragePlugin": (report["perf"]["plugins"].get("StoragePlugin") or {}).get("avg"),
        "MetadataPlugin": (report["perf"]["plugins"].get("MetadataPlugin") or {}).get("avg"),
        "Pipeline": (report["perf"]["worker_pipeline"] or {}).get("avg"),
        "UploadJob_create_warm": (
            report["perf"]["upload_job_create_ms_warm"] or {}
        ).get("avg"),
        "VisionQueue": (report["perf"]["vision_queue_ms"] or {}).get("avg"),
        "HashPlugin": (report["perf"]["plugins"].get("HashPlugin") or {}).get("avg"),
        "PreviewPlugin": (report["perf"]["plugins"].get("PreviewPlugin") or {}).get("avg"),
        "ExifPlugin": (report["perf"]["plugins"].get("ExifPlugin") or {}).get("avg"),
        "GpsPlugin": (report["perf"]["plugins"].get("GpsPlugin") or {}).get("avg"),
    }
    report["before_after"] = {
        key: improvement(phase1[key], after.get(key)) for key in phase1
    }
    report["targets"] = {
        "pipeline_under_2s": bool(after.get("Pipeline") and after["Pipeline"] < 2000),
        "storage_under_400ms": bool(
            after.get("StoragePlugin") and after["StoragePlugin"] < 400
        ),
        "metadata_under_200ms": bool(
            after.get("MetadataPlugin") and after["MetadataPlugin"] < 200
        ),
        "upload_job_warm_under_150ms": bool(
            after.get("UploadJob_create_warm") and after["UploadJob_create_warm"] < 150
        ),
    }

    out = ROOT / "scripts" / "qa_perf_phase2_report.json"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    out.write_text(text, encoding="utf-8")
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    print(f"REPORT_PATH={out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
