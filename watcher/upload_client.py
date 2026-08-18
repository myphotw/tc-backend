"""Upload API Multipart Client."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)


class UploadClientError(Exception):
    """Upload API 호출 실패."""


class UploadClient:
    """기존 /api/common/upload 를 호출하는 클라이언트."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8000",
        timeout: int = 60,
        retry_count: int = 3,
        retry_delay: float = 2.0,
        auth_token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.upload_url = f"{self.base_url}/api/common/upload"
        self.timeout = timeout
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        configured_token = (
            auth_token
            if auth_token is not None
            else os.environ.get("TC_BACKEND_AUTH_TOKEN")
        )
        self.auth_token = (
            configured_token
            if configured_token is not None and configured_token.strip()
            else None
        )

    def upload(self, file_path: Path) -> dict[str, Any]:
        """
        Multipart로 파일을 업로드한다.

        Args:
            file_path: 업로드할 파일 경로

        Returns:
            dict[str, Any]: Upload API 응답
        """
        last_error: Exception | None = None
        for attempt in range(1, self.retry_count + 1):
            try:
                logger.info("UPLOAD_START path=%s attempt=%s", file_path, attempt)
                headers = (
                    {"Authorization": f"Bearer {self.auth_token}"}
                    if self.auth_token is not None
                    else None
                )
                with file_path.open("rb") as handle:
                    response = requests.post(
                        self.upload_url,
                        files={
                            "file": (file_path.name, handle),
                        },
                        headers=headers,
                        timeout=self.timeout,
                    )
                if response.status_code >= 400:
                    raise UploadClientError(
                        f"Upload API status={response.status_code} body={response.text}"
                    )
                payload = response.json()
                logger.info(
                    "UPLOAD_COMPLETE path=%s job_id=%s",
                    file_path,
                    payload.get("job_id"),
                )
                return payload
            except (requests.RequestException, UploadClientError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "UPLOAD_FAILED path=%s attempt=%s/%s error=%s",
                    file_path,
                    attempt,
                    self.retry_count,
                    exc,
                )
                if attempt < self.retry_count:
                    time.sleep(self.retry_delay)

        raise UploadClientError(
            f"Upload failed after {self.retry_count} retries: {last_error}"
        ) from last_error
