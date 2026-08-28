"""UploadWorker identity helpers."""

from __future__ import annotations

import os
import socket
import uuid


def _create_upload_worker_id() -> str:
    """프로세스 시작 시 사용할 고유 UploadWorker ID를 생성한다."""
    configured = (os.environ.get("UPLOAD_WORKER_ID") or "").strip()
    if configured:
        return configured
    host = socket.gethostname().replace(" ", "-") or "host"
    return f"UploadWorker-{host}-{os.getpid()}-{uuid.uuid4().hex}"


_UPLOAD_WORKER_ID = _create_upload_worker_id()


def resolve_upload_worker_id() -> str:
    """
    UploadWorker 고유 ID를 반환한다.

    우선순위:
    1. UPLOAD_WORKER_ID 환경변수
    2. UploadWorker-{hostname}-{pid}-{process_instance_uuid}

    모듈 초기화 시 한 번 생성한 값을 반환하므로 프로세스 수명 동안 고정된다.
    """
    return _UPLOAD_WORKER_ID


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
