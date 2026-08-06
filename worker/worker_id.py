"""UploadWorker identity helpers."""

from __future__ import annotations

import os
import socket


def resolve_upload_worker_id() -> str:
    """
    UploadWorker 고유 ID를 반환한다.

    우선순위:
    1. UPLOAD_WORKER_ID 환경변수
    2. UploadWorker-{hostname}-{pid}
    """
    configured = (os.environ.get("UPLOAD_WORKER_ID") or "").strip()
    if configured:
        return configured
    host = socket.gethostname().replace(" ", "-") or "host"
    return f"UploadWorker-{host}-{os.getpid()}"


def resolve_stale_seconds(default: int = 300) -> int:
    """UPLOAD_JOB_STALE_SECONDS 환경변수 (기본 300)."""
    raw = os.environ.get("UPLOAD_JOB_STALE_SECONDS")
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)
