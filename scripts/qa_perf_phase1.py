"""QA Phase1: API / Upload Pipeline 성능 계측 시나리오 실행.

기능/응답/스키마를 변경하지 않고 소요시간만 측정한다.
"""

from __future__ import annotations

import io
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

# Ensure app imports resolve before configuring logging handlers.
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


def make_jpg_bytes(size: tuple[int, int] = (1200, 800), color=(40, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def summarize(values: list[float]) -> dict[str, float | None]:
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


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from qa_env_guard import require_test_environment

    require_test_environment(script_name="qa_perf_phase1")

    from fastapi.testclient import TestClient

    # httpx may be missing even with FastAPI; install guidance if import fails.
    try:
        import httpx  # noqa: F401
    except ImportError:
        print("httpx missing; installing for TestClient...")
        import subprocess

        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "httpx"],
            cwd=str(ROOT),
        )

    from app.main import app
    from app.common.database import SessionLocal
    from app.common.models.file import CommonFile
    from app.common.models.upload_job import UploadJob
    from app.common.repositories.upload_job_repository import UploadJobRepository
    from worker.background_worker import process_upload_job
    from worker.vision_worker import process_vision_job
    from app.common.models.vision_job import CommonVisionJob

    capture = PerfCapture()
    logging.getLogger("tc.perf").addHandler(capture)
    logging.getLogger("tc.perf").setLevel(logging.INFO)

    client = TestClient(app)
    # Unique JPEGs so HashPlugin does not short-circuit all B jobs as duplicates.
    jpg = make_jpg_bytes(color=(12, 34, 200))
    report: dict = {"scenarios": {}, "notes": []}

    def header_ms(resp) -> float | None:
        raw = resp.headers.get("x-process-time-ms")
        return float(raw) if raw else None

    def upload_one(name: str, payload: bytes | None = None) -> tuple[dict, float | None]:
        body = payload if payload is not None else jpg
        files = {"file": (name, body, "image/jpeg")}
        started = time.perf_counter()
        resp = client.post("/api/common/upload", files=files)
        wall = round((time.perf_counter() - started) * 1000, 2)
        resp.raise_for_status()
        return resp.json(), header_ms(resp) or wall

    # --- A: JPG 1장 업로드 ---
    a_times: list[float] = []
    job_payload, ms = upload_one("qa_a.jpg")
    a_times.append(ms)
    report["scenarios"]["A_upload_1"] = {
        **summarize(a_times),
        "bottleneck": "incoming_save (see upload_api perf)",
        "job_id": job_payload.get("job_id"),
    }

    # Process A through worker for plugin timings
    db = SessionLocal()
    try:
        job = (
            db.query(UploadJob)
            .filter(UploadJob.job_id == job_payload["job_id"])
            .first()
        )
        if job is not None:
            UploadJobRepository(db).mark_processing(job)
            process_upload_job(db, job)
    except Exception as exc:
        report["notes"].append(f"Worker process A failed: {exc}")
        db.rollback()
    finally:
        db.close()

    # Optional Vision sample (separate from Upload completion)
    db = SessionLocal()
    try:
        vjob = (
            db.query(CommonVisionJob)
            .filter(CommonVisionJob.status == "WAITING")
            .order_by(CommonVisionJob.id.desc())
            .first()
        )
        if vjob is not None:
            from app.common.repositories.vision_job_repository import VisionJobRepository

            VisionJobRepository(db).mark_processing(vjob)
            process_vision_job(db, vjob)
        else:
            report["notes"].append("Vision job skip: no WAITING job")
    except Exception as exc:
        report["notes"].append(f"Vision process failed: {exc}")
        db.rollback()
    finally:
        db.close()

    # --- B: JPG 10장 연속 업로드 ---
    b_times: list[float] = []
    b_jobs: list[str] = []
    for i in range(10):
        unique = make_jpg_bytes(color=(40 + i * 10, 80 + i * 5, 160 + i * 3))
        payload, ms = upload_one(f"qa_b_{i}.jpg", unique)
        b_times.append(ms)
        b_jobs.append(str(payload.get("job_id")))
    report["scenarios"]["B_upload_10"] = {
        **summarize(b_times),
        "bottleneck": "incoming_save / multipart",
        "job_ids_n": len(b_jobs),
    }

    # Process first of B for additional plugin sample (avoid 10x long geocode/vision)
    db = SessionLocal()
    try:
        if b_jobs:
            job = db.query(UploadJob).filter(UploadJob.job_id == b_jobs[0]).first()
            if job is not None and job.status == "WAITING":
                UploadJobRepository(db).mark_processing(job)
                process_upload_job(db, job)
    except Exception as exc:
        report["notes"].append(f"Worker process B[0] failed: {exc}")
        db.rollback()
    finally:
        db.close()

    # Gallery endpoints
    def timed_get(path: str, n: int = 3) -> list[float]:
        values: list[float] = []
        for _ in range(n):
            started = time.perf_counter()
            resp = client.get(path)
            wall = round((time.perf_counter() - started) * 1000, 2)
            values.append(header_ms(resp) or wall)
            if resp.status_code >= 400:
                report["notes"].append(f"{path} status={resp.status_code}")
        return values

    report["scenarios"]["C_gallery_page"] = {
        **summarize(timed_get("/api/common/gallery?page=1&page_size=20")),
        "bottleneck": "db_query (count+rows+ai_tag_flag)",
    }
    report["scenarios"]["D_search"] = {
        **summarize(timed_get("/api/common/gallery/search?page=1&page_size=20")),
        "bottleneck": "db_query / filters",
    }
    report["scenarios"]["E_map"] = {
        **summarize(timed_get("/api/common/gallery/map")),
        "bottleneck": "db_query (GPS join)",
    }
    report["scenarios"]["F_timeline"] = {
        **summarize(timed_get("/api/common/gallery/timeline")),
        "bottleneck": "db_query aggregate",
    }
    report["scenarios"]["G_statistics"] = {
        **summarize(timed_get("/api/common/gallery/statistics")),
        "bottleneck": "multiple aggregate queries",
    }

    # Media samples from DB
    db = SessionLocal()
    try:
        files = (
            db.query(CommonFile)
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFile.thumb_path.isnot(None))
            .order_by(CommonFile.id.desc())
            .limit(10)
            .all()
        )
    finally:
        db.close()

    thumb_times: list[float] = []
    for f in files:
        started = time.perf_counter()
        resp = client.get(f"/api/common/gallery/{f.file_id}/thumbnail")
        wall = round((time.perf_counter() - started) * 1000, 2)
        thumb_times.append(header_ms(resp) or wall)
        if resp.status_code >= 400:
            report["notes"].append(f"thumbnail {f.file_id} status={resp.status_code}")
    report["scenarios"]["H_thumbnail_10"] = {
        **summarize(thumb_times),
        "bottleneck": "db_lookup + FileResponse stream",
        "cache_control": "public, max-age=86400",
    }

    if files:
        fid = files[0].file_id
        report["scenarios"]["I_preview_1"] = {
            **summarize(timed_get(f"/api/common/gallery/{fid}/preview", n=1)),
            "bottleneck": "db_lookup + stream",
        }
        report["scenarios"]["J_original_1"] = {
            **summarize(timed_get(f"/api/common/gallery/{fid}/original", n=1)),
            "bottleneck": "db_lookup + larger stream",
        }
    else:
        report["notes"].append("No CommonFile with thumb_path for media scenarios")

    # Aggregate perf events
    by_event: dict[str, list[dict[str, str]]] = defaultdict(list)
    for ev in capture.events:
        by_event[ev.get("event", "?")].append(ev)

    def collect_ms(event_name: str, field: str = "elapsed_ms") -> list[float]:
        out: list[float] = []
        for ev in by_event.get(event_name, []):
            v = parse_float(ev, field)
            if v is not None:
                out.append(v)
        return out

    query_counts = {
        "gallery_list": [parse_float(e, "query_count") for e in by_event.get("gallery_list", [])],
        "gallery_search": [parse_float(e, "query_count") for e in by_event.get("gallery_search", [])],
        "gallery_map": [parse_float(e, "query_count") for e in by_event.get("gallery_map", [])],
        "gallery_timeline": [
            parse_float(e, "query_count") for e in by_event.get("gallery_timeline", [])
        ],
        "gallery_statistics": [
            parse_float(e, "query_count") for e in by_event.get("gallery_statistics", [])
        ],
        "gallery_detail": [
            parse_float(e, "query_count") for e in by_event.get("gallery_detail", [])
        ],
    }
    report["api_query_counts"] = {
        k: [int(v) for v in vals if v is not None] for k, vals in query_counts.items()
    }

    plugin_ms: dict[str, list[float]] = defaultdict(list)
    for ev in by_event.get("worker_plugin", []):
        name = ev.get("plugin")
        ms = parse_float(ev, "elapsed_ms")
        if name and ms is not None:
            plugin_ms[name].append(ms)
    report["plugin_elapsed_ms"] = {k: summarize(v) for k, v in plugin_ms.items()}

    report["upload_api"] = summarize(collect_ms("upload_api"))
    report["upload_worker"] = summarize(collect_ms("upload_worker_job"))
    report["worker_pipeline"] = summarize(collect_ms("worker_pipeline"))
    report["vision"] = {
        "vision_plugin": summarize(collect_ms("vision_plugin")),
        "vision_worker_job": summarize(collect_ms("vision_worker_job")),
        "geocoding": by_event.get("geocoding", [])[:5],
    }

    # N+1 note from code path (list uses batch AI tag query)
    report["n_plus_one"] = {
        "gallery_list_search": (
            "No per-row N+1 for metadata/GPS/favorite/tags existence: "
            "outerjoin metadata + favorite column + single IN() AI-tag flag query. "
            "Typical query_count ~= 3 (count + rows + ai_tag_flag)."
        ),
        "gallery_detail": "3 queries (file+metadata, tags, history_count)",
        "gallery_statistics": "Multiple aggregate queries (not per-photo N+1)",
    }

    # Bottlenecks from measured averages
    candidates: list[tuple[str, float]] = []
    for name, data in report["scenarios"].items():
        avg = data.get("avg")
        if isinstance(avg, (int, float)):
            candidates.append((name, float(avg)))
    for plugin, data in report["plugin_elapsed_ms"].items():
        avg = data.get("avg")
        if isinstance(avg, (int, float)):
            candidates.append((f"plugin:{plugin}", float(avg)))
    for label, data in (
        ("upload_api", report["upload_api"]),
        ("upload_worker", report["upload_worker"]),
        ("worker_pipeline", report["worker_pipeline"]),
    ):
        avg = data.get("avg")
        if isinstance(avg, (int, float)):
            candidates.append((label, float(avg)))
    candidates.sort(key=lambda x: x[1], reverse=True)
    report["top_bottlenecks"] = candidates[:5]

    report["optimization_priority"] = [
        "1. Upload Worker heavy plugins (Preview/Storage/Hash) - sequential CPU/IO",
        "2. Gallery statistics multiple aggregate queries - consolidate if slow",
        "3. Gallery list/search count()+rows separate queries - consider window count",
        "4. Media endpoint DB lookup per request - optional path cache later",
        "5. Vision/Geocoding external API latency (separate from upload completion)",
    ]

    import json

    out_path = ROOT / "scripts" / "qa_perf_phase1_report.json"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    out_path.write_text(text, encoding="utf-8")
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    print(f"REPORT_PATH={out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
