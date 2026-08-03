"""공통 외부 API Client 기반 클래스."""

from __future__ import annotations

import logging
from typing import Any

import requests
from requests import Response, Session
from sqlalchemy.orm import Session as DbSession

from app.common.config import settings
from app.common.repositories.api_usage_repository import ApiUsageRepository

logger = logging.getLogger(__name__)


class ApiClientError(Exception):
    """외부 API Client 공통 예외."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class BaseClient:
    """
    외부 API 호출을 위한 공통 Client.

    Plugin은 requests를 직접 사용하지 않고
    이 Client 계층만 호출한다.

    API 호출은 ApiUsageRepository를 거쳐 사용량을 관리한다.
    현재 Usage 증가는 Mock으로만 처리한다.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: int | None = None,
        retry_count: int | None = None,
        session: Session | None = None,
        db: DbSession | None = None,
        provider: str | None = None,
        api_name: str | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout if timeout is not None else settings.API_CLIENT_TIMEOUT
        self.retry_count = (
            retry_count
            if retry_count is not None
            else settings.API_CLIENT_RETRY_COUNT
        )
        self.session = session or requests.Session()
        self.db = db
        self.provider = provider
        self.api_name = api_name
        self.logger = logging.getLogger(self.__class__.__name__)

    def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """공통 GET 요청."""
        return self._request(
            method="GET",
            path=path,
            params=params,
            headers=headers,
        )

    def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """공통 POST 요청."""
        return self._request(
            method="POST",
            path=path,
            params=params,
            json=json,
            data=data,
            headers=headers,
        )

    def close(self) -> None:
        """Session을 종료한다."""
        self.session.close()

    def track_usage(self, *, units: int = 1) -> None:
        """
        API Usage를 Mock 증가시킨다.

        Vision Worker는 can_use()로 limit을 확인한 뒤
        이 메서드 또는 ApiUsageRepository.increase_usage()를 사용할 수 있다.
        """
        if self.db is None or not self.provider or not self.api_name:
            return
        ApiUsageRepository(self.db).increase_usage(
            provider=self.provider,
            api_name=self.api_name,
            units=units,
        )

    def can_use(self, *, units: int = 1) -> bool:
        """Usage limit 내 호출 가능 여부를 확인한다."""
        if self.db is None or not self.provider or not self.api_name:
            return True
        return ApiUsageRepository(self.db).can_use(
            provider=self.provider,
            api_name=self.api_name,
            units=units,
        )

    def _request(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        timeout / retry / usage 확인이 적용된 공통 요청 처리.

        현재 단계에서는 실제 외부 API 호출 구조를 제공하며,
        Usage 증가는 Mock으로 기록한다.
        """
        if not self.can_use(units=1):
            raise ApiClientError(
                f"API usage limit exceeded: provider={self.provider} api_name={self.api_name}"
            )

        url = self._build_url(path)
        last_error: Exception | None = None

        for attempt in range(1, self.retry_count + 1):
            try:
                self.logger.info(
                    "API request method=%s url=%s attempt=%s/%s",
                    method,
                    url,
                    attempt,
                    self.retry_count,
                )
                response = self.session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json,
                    data=data,
                    headers=headers,
                    timeout=self.timeout,
                )
                payload = self._parse_response(response)
                self.track_usage(units=1)
                return payload
            except (requests.RequestException, ApiClientError) as exc:
                last_error = exc
                self.logger.warning(
                    "API request failed method=%s url=%s attempt=%s error=%s",
                    method,
                    url,
                    attempt,
                    exc,
                )

        raise ApiClientError(
            f"API request failed after {self.retry_count} retries: {last_error}"
        ) from last_error

    def _build_url(self, path: str) -> str:
        """base_url과 path를 결합한다."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not self.base_url:
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _parse_response(self, response: Response) -> dict[str, Any]:
        """HTTP 응답을 dict로 변환한다."""
        if response.status_code >= 400:
            raise ApiClientError(
                f"API responded with status={response.status_code}",
                status_code=response.status_code,
                response_text=response.text,
            )

        if not response.content:
            return {}

        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiClientError(
                "API response is not valid JSON",
                status_code=response.status_code,
                response_text=response.text,
            ) from exc

        if isinstance(payload, dict):
            return payload
        return {"data": payload}
