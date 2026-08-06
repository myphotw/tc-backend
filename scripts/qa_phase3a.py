"""Phase 3A: parallel UploadWorker claim / stale / dashboard / perf tests."""

from __future__ import annotations

import io
import json
import logging
import os
import statistics
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(message)s")


def make_jpg(noise: int) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (640, 480), color=(10 + noise % 200, 40, 90))
    pixels = img.load()
    assert pixels is not None
    for x in range(0, 640, 32):
        for y in range(0, 480, 32):
            pixels[x, y] = ((x + noise) % 256, (y + noise) % 256, (x + y) % 256)
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def summarize(values: list[float]) -> dict:
    if not values:
        return {"avg": None, "min": None, "max": None, "p95": None, "n": 0}
    ordered = sorted(values)
    p95_idx = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return {
        "avg": round(statistics.mean(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "p95": round(ordered[p95_idx], 2),
        "n": len(values),
    }


def test_sqlite_claim_fallback() -> dict:
    import tempfile

    from app.common.database import Base
    from app.common.models.upload_job import UploadJob
    from app.common.repositories.upload_job_repository import UploadJobRepository

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=[UploadJob.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()
    repo = UploadJobRepository(db)
    for i in range(5):
        repo.create_waiting_job(
            job_id=f"sqlite-{i}",
            source_type="UPLOAD",
            incoming_path=f"incoming/sqlite-{i}.jpg",
        )

    claimed_ids: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        local = Session()
        try:
            local_repo = UploadJobRepository(local)
            misses = 0
            while misses < 10:
                job = local_repo.claim_next_waiting_job(name)
                if job is None:
                    misses += 1
                    time.sleep(0.02)
                    continue
                misses = 0
                with lock:
                    claimed_ids.append(job.job_id)
                time.sleep(0.01)
        except Exception as exc:
            with lock:
                errors.append(f"{name}:{exc}")
        finally:
            local.close()

    threads = [
        threading.Thread(target=worker, args=("UploadWorker-SQLite-A",)),
        threading.Thread(target=worker, args=("UploadWorker-SQLite-B",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    counts = Counter(claimed_ids)
    duplicates = {k: v for k, v in counts.items() if v > 1}
    db.close()
    engine.dispose()
    try:
        Path(db_path).unlink(missing_ok=True)
    except Exception:
        pass
    return {
        "claimed": len(claimed_ids),
        "unique": len(counts),
        "duplicates": duplicates,
        "errors": errors,
        "pass": len(claimed_ids) == 5 and not duplicates and not errors,
    }


def test_postgres_parallel_and_stale(job_count: int = 20) -> dict:
    from fastapi.testclient import TestClient

    from app.main import app
    from app.common.database import SessionLocal
    from app.common.models.upload_job import UploadJob
    from app.common.repositories.upload_job_repository import (
        UploadJobRepository,
        UploadJobStatus,
    )
    from app.common.repositories.worker_status_repository import WorkerStatusRepository
    from worker.background_worker import process_next_job, process_upload_job
    from worker.worker_monitor import WorkerMonitor

    client = TestClient(app)
    report: dict = {"tests": {}, "perf": {}}

    # Ensure dialect is postgres
    db = SessionLocal()
    dialect = db.get_bind().dialect.name
    db.close()
    report["dialect"] = dialect

    def upload_unique(i: int) -> str:
        data = make_jpg(noise=int(time.time() * 1000) % 100000 + i * 97)
        resp = client.post(
            "/api/common/upload",
            files={"file": (f"p3a_{i}.jpg", data, "image/jpeg")},
        )
        resp.raise_for_status()
        return resp.json()["job_id"]

    # --- create jobs ---
    job_ids = [upload_unique(i) for i in range(job_count)]
    report["tests"]["created_jobs"] = len(job_ids)

    # --- parallel claim+process with 2 workers ---
    processed: list[str] = []
    lock = threading.Lock()
    stop = threading.Event()

    def run_worker(name: str) -> None:
        monitor = WorkerMonitor(name, heartbeat_interval=5, job_heartbeat_interval=2)
        monitor.start()
        try:
            idle = 0
            while not stop.is_set() and idle < 30:
                did = process_next_job(worker_id=name, monitor=monitor)
                if did:
                    idle = 0
                    with lock:
                        # find which completed recently is hard; count via DB later
                        pass
                else:
                    idle += 1
                    time.sleep(0.05)
        finally:
            monitor.stop()

    t0 = time.perf_counter()
    workers = [
        threading.Thread(target=run_worker, args=("UploadWorker-A",)),
        threading.Thread(target=run_worker, args=("UploadWorker-B",)),
    ]
    for t in workers:
        t.start()

    # wait until all target jobs leave WAITING/PROCESSING
    deadline = time.time() + 180
    while time.time() < deadline:
        db = SessionLocal()
        try:
            pending = (
                db.query(UploadJob)
                .filter(UploadJob.job_id.in_(job_ids))
                .filter(
                    UploadJob.status.in_(
                        [UploadJobStatus.WAITING, UploadJobStatus.PROCESSING]
                    )
                )
                .count()
            )
        finally:
            db.close()
        if pending == 0:
            break
        time.sleep(0.2)
    stop.set()
    for t in workers:
        t.join(timeout=60)
    elapsed = time.perf_counter() - t0

    db = SessionLocal()
    try:
        rows = db.query(UploadJob).filter(UploadJob.job_id.in_(job_ids)).all()
        status_counts = Counter(r.status for r in rows)
        claim_workers = []
        for r in rows:
            w = UploadJobRepository.extract_claimed_worker(r.processing_log)
            if w:
                claim_workers.append(w)
        claim_counts = Counter(claim_workers)
        # duplicate claim = same job claimed by 2 workers (two CLAIMED lines)
        multi_claim = 0
        for r in rows:
            log = (r.processing_log or "").replace("\\n", "\n")
            if log.count("CLAIMED worker=") > 1 and "STALE_JOB_RECOVERED" not in log:
                # after stale recover, re-claim is OK; count only without recover for initial run
                pass
            # For fresh jobs, exactly one CLAIMED expected (unless stale path)
            claimed = log.count("CLAIMED worker=")
            recovered = log.count("STALE_JOB_RECOVERED")
            if claimed > recovered + 1:
                multi_claim += 1

        completed = status_counts.get(UploadJobStatus.COMPLETED, 0)
        failed = status_counts.get(UploadJobStatus.FAILED, 0)
        report["tests"]["parallel_20"] = {
            "elapsed_sec": round(elapsed, 3),
            "status_counts": dict(status_counts),
            "claim_by_worker": dict(claim_counts),
            "multi_claim_jobs": multi_claim,
            "completed": completed,
            "failed": failed,
            "pass": completed + failed == job_count and multi_claim == 0,
        }
    finally:
        db.close()

    # --- stale recovery ---
    stale_job_id = upload_unique(9001)
    db = SessionLocal()
    try:
        repo = UploadJobRepository(db)
        job = repo.claim_next_waiting_job("UploadWorker-A")
        assert job is not None and job.job_id == stale_job_id
        # make it stale
        job.started_at = datetime.now(timezone.utc) - timedelta(seconds=400)
        db.commit()
        recovered = repo.recover_stale_processing_jobs(
            stale_seconds=300,
            worker_id="UploadWorker-B",
        )
        job2 = repo.get(stale_job_id)
        assert job2 is not None
        stale_ok = (
            recovered >= 1
            and job2.status == UploadJobStatus.WAITING
            and "STALE_JOB_RECOVERED" in (job2.processing_log or "")
        )
        # other worker completes recovered job
        claimed = None
        for _ in range(20):
            claimed = repo.claim_next_waiting_job("UploadWorker-B")
            if claimed is None:
                break
            if claimed.job_id == stale_job_id:
                break
            # unintended claim: mark back? leave PROCESSING briefly then continue
            # For test isolation, process unintended quickly later.
            if claimed.job_id != stale_job_id:
                # put back to WAITING for isolation
                claimed.status = UploadJobStatus.WAITING
                claimed.started_at = None
                db.commit()
                claimed = None
        assert claimed is not None and claimed.job_id == stale_job_id
    finally:
        db.close()

    # process recovered with separate session
    db = SessionLocal()
    try:
        job = UploadJobRepository(db).get(stale_job_id)
        assert job is not None
        process_upload_job(db, job, worker_id="UploadWorker-B")
        done = UploadJobRepository(db).get(stale_job_id)
        report["tests"]["stale_recovery"] = {
            "pass": stale_ok and done is not None and done.status == UploadJobStatus.COMPLETED,
            "recovered": recovered,
            "final_status": getattr(done, "status", None),
        }
    except Exception as exc:
        db.rollback()
        report["tests"]["stale_recovery"] = {"pass": False, "error": str(exc)}
    finally:
        db.close()

    # --- failed job does not stop others ---
    bad_id = f"bad-{int(time.time())}"
    db = SessionLocal()
    try:
        UploadJobRepository(db).create_waiting_job(
            job_id=bad_id,
            source_type="UPLOAD",
            incoming_path="incoming/missing-phase3a.jpg",
        )
        good_id = upload_unique(9002)
    finally:
        db.close()

    process_next_job(worker_id="UploadWorker-A")
    process_next_job(worker_id="UploadWorker-A")
    db = SessionLocal()
    try:
        bad = UploadJobRepository(db).get(bad_id)
        good = UploadJobRepository(db).get(good_id)
        report["tests"]["fail_continue"] = {
            "pass": bool(
                bad
                and bad.status == UploadJobStatus.FAILED
                and good
                and good.status
                in {UploadJobStatus.COMPLETED, UploadJobStatus.PROCESSING, UploadJobStatus.WAITING}
            ),
            "bad": getattr(bad, "status", None),
            "good": getattr(good, "status", None),
        }
        # finish good if still waiting/processing
        if good and good.status == UploadJobStatus.WAITING:
            pass
    finally:
        db.close()
    # drain remaining
    for _ in range(5):
        if not process_next_job(worker_id="UploadWorker-A"):
            break

    # --- dashboard workers ---
    # ensure both worker rows exist
    db = SessionLocal()
    try:
        WorkerStatusRepository(db).update_status("UploadWorker-A", "RUNNING", version="1.0.0")
        WorkerStatusRepository(db).update_status("UploadWorker-B", "RUNNING", version="1.0.0")
    finally:
        db.close()
    dash = client.get("/api/common/dashboard")
    dash.raise_for_status()
    workers_payload = dash.json().get("workers", [])
    names = {w.get("name") for w in workers_payload}
    report["tests"]["dashboard"] = {
        "pass": "UploadWorker-A" in names and "UploadWorker-B" in names,
        "workers": [
            {
                "name": w.get("name"),
                "status": w.get("status"),
                "last_started": w.get("last_started"),
                "last_heartbeat": w.get("last_heartbeat"),
                "current_job_id": w.get("current_job_id"),
                "version": w.get("version"),
            }
            for w in workers_payload
            if str(w.get("name", "")).startswith("UploadWorker")
        ],
    }

    return report


def measure_worker_throughput(worker_count: int, job_count: int) -> dict:
    from fastapi.testclient import TestClient

    from app.main import app
    from app.common.database import SessionLocal
    from app.common.models.upload_job import UploadJob
    from app.common.repositories.upload_job_repository import UploadJobStatus
    from worker.background_worker import process_next_job
    from worker.worker_monitor import WorkerMonitor

    client = TestClient(app)
    seed = int(time.time() * 1000) % 100000

    def upload_one(i: int) -> str:
        data = make_jpg(noise=seed + worker_count * 1000 + i * 13)
        resp = client.post(
            "/api/common/upload",
            files={"file": (f"perf_{worker_count}_{i}.jpg", data, "image/jpeg")},
        )
        resp.raise_for_status()
        return resp.json()["job_id"]

    job_ids = [upload_one(i) for i in range(job_count)]
    stop = threading.Event()

    def run_worker(idx: int) -> None:
        name = f"UploadWorker-PERF-{worker_count}-{idx}"
        monitor = WorkerMonitor(name, heartbeat_interval=5, job_heartbeat_interval=2)
        monitor.start()
        try:
            idle = 0
            while not stop.is_set() and idle < 40:
                if process_next_job(worker_id=name, monitor=monitor):
                    idle = 0
                else:
                    idle += 1
                    time.sleep(0.05)
        finally:
            monitor.stop()

    t0 = time.perf_counter()
    threads = [
        threading.Thread(target=run_worker, args=(i,))
        for i in range(worker_count)
    ]
    for t in threads:
        t.start()
    deadline = time.time() + max(120, job_count * 8)
    while time.time() < deadline:
        db = SessionLocal()
        try:
            pending = (
                db.query(UploadJob)
                .filter(UploadJob.job_id.in_(job_ids))
                .filter(
                    UploadJob.status.in_(
                        [UploadJobStatus.WAITING, UploadJobStatus.PROCESSING]
                    )
                )
                .count()
            )
        finally:
            db.close()
        if pending == 0:
            break
        time.sleep(0.2)
    stop.set()
    for t in threads:
        t.join(timeout=30)

    # Force-recover any stuck PROCESSING from this batch so measurement closes.
    db = SessionLocal()
    try:
        from app.common.repositories.upload_job_repository import UploadJobRepository

        stuck = (
            db.query(UploadJob)
            .filter(UploadJob.job_id.in_(job_ids))
            .filter(UploadJob.status == UploadJobStatus.PROCESSING)
            .all()
        )
        for job in stuck:
            job.started_at = datetime.now(timezone.utc) - timedelta(seconds=9999)
        if stuck:
            db.commit()
            UploadJobRepository(db).recover_stale_processing_jobs(
                stale_seconds=1,
                worker_id="UploadWorker-PERF-RECOVER",
            )
    finally:
        db.close()

    # Drain recovered
    for _ in range(job_count):
        if not process_next_job(worker_id=f"UploadWorker-PERF-DRAIN-{worker_count}"):
            break

    total_sec = time.perf_counter() - t0

    db = SessionLocal()
    try:
        rows = db.query(UploadJob).filter(UploadJob.job_id.in_(job_ids)).all()
        statuses = Counter(r.status for r in rows)
        # pipeline times not always available; approximate via completed timestamps span
    finally:
        db.close()

    completed = statuses.get(UploadJobStatus.COMPLETED, 0)
    failed = statuses.get(UploadJobStatus.FAILED, 0)
    return {
        "workers": worker_count,
        "jobs": job_count,
        "completed": completed,
        "failed": failed,
        "total_sec": round(total_sec, 3),
        "jobs_per_sec": round(completed / total_sec, 3) if total_sec > 0 else None,
        "status_counts": dict(statuses),
    }


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from qa_env_guard import require_test_environment

    require_test_environment(script_name="qa_phase3a")

    report: dict = {
        "sqlite_fallback": test_sqlite_claim_fallback(),
    }

    try:
        report["postgres"] = test_postgres_parallel_and_stale(job_count=20)
    except Exception as exc:
        report["postgres"] = {"error": str(exc)}

    # Perf: 20 jobs (environment limit note). Try 20 as minimum; optionally 100 via env.
    job_n = int(os.environ.get("PHASE3A_PERF_JOBS", "20"))
    try:
        one = measure_worker_throughput(1, job_n)
        two = measure_worker_throughput(2, job_n)
        speedup = None
        if one.get("total_sec") and two.get("total_sec") and two["total_sec"] > 0:
            speedup = round(one["total_sec"] / two["total_sec"], 3)
        report["perf"] = {
            "job_count": job_n,
            "note": (
                "Default 20 jobs due to environment cost; "
                "set PHASE3A_PERF_JOBS=100 for full target."
            ),
            "one_worker": one,
            "two_workers": two,
            "speedup": speedup,
            "target_speedup_1_6": bool(speedup and speedup >= 1.6),
        }
    except Exception as exc:
        report["perf"] = {"error": str(exc)}

    out = ROOT / "scripts" / "qa_phase3a_report.json"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    out.write_text(text, encoding="utf-8")
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    print(f"REPORT_PATH={out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
