"""Astrometry.net API Client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests
from requests import Session as HttpSession
from sqlalchemy.orm import Session

from app.common.config import settings
from app.common.repositories.api_usage_repository import ApiName, ApiProvider
from app.common.services.api_clients.base_client import (
    ApiClientError,
    BaseClient,
    ExternalApiErrorCode,
)


class AstrometryProviderWorkNotFound(ApiClientError):
    """The provider explicitly reports that a saved submission or job is gone."""

    def __init__(self, *, resource: str, provider_id: int) -> None:
        super().__init__(
            f"Astrometry {resource} was not found: {provider_id}",
            status_code=404,
        )
        self.resource = resource
        self.provider_id = provider_id


class AstrometryClient(BaseClient):
    """
    Astrometry.net Plate Solve API Client.

    Implements login, multipart upload and provider-side job status polling.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        db: Session | None = None,
        session: HttpSession | None = None,
    ) -> None:
        super().__init__(
            base_url="https://nova.astrometry.net",
            db=db,
            provider=ApiProvider.ASTROMETRY,
            api_name=ApiName.PLATESOLVE,
            session=session,
        )
        self.api_key = api_key if api_key is not None else settings.ASTROMETRY_API_KEY

    def solve(self, *, image_path: str) -> dict[str, Any]:
        """
        천체 이미지 Plate Solve를 수행한다.

        Args:
            image_path: Plate Solve 대상 이미지 경로

        Returns:
            dict[str, Any]: submitted provider job
        """
        return self.submit(image_path=image_path)

    def submit(self, *, image_path: str) -> dict[str, Any]:
        if not self.api_key:
            raise ApiClientError(
                "Astrometry API key is not configured",
                code=ExternalApiErrorCode.API_KEY_NOT_CONFIGURED,
            )
        path = Path(image_path)
        if not path.is_file():
            raise ApiClientError(
                "Plate solve image file was not found",
                code=ExternalApiErrorCode.INVALID_REQUEST,
            )

        login = self.post(
            "/api/login",
            data={"request-json": json.dumps({"apikey": self.api_key})},
        )
        session_key = login.get("session")
        if login.get("status") != "success" or not session_key:
            raise ApiClientError("Astrometry authentication failed")

        request_json = json.dumps(
            {
                "session": session_key,
                "publicly_visible": "n",
                "allow_modifications": "d",
                "allow_commercial_use": "d",
            }
        )
        with path.open("rb") as handle:
            uploaded = self.post(
                "/api/upload",
                files={
                    "request-json": (None, request_json, "text/plain"),
                    "file": (path.name, handle, "application/octet-stream"),
                },
            )
        submission_id = uploaded.get("subid")
        if uploaded.get("status") != "success" or submission_id is None:
            raise ApiClientError("Astrometry image upload failed")
        self.track_usage(units=1)
        return {
            "provider": "astrometry.net",
            "status": "WAITING",
            "submission_id": int(submission_id),
        }

    def get_status(self, *, submission_id: int) -> dict[str, Any]:
        """Legacy combined status lookup used by the synchronous API path."""
        submission = self.get_submission_status(submission_id=submission_id)
        provider_job_id = submission.get("provider_job_id")
        if provider_job_id is None:
            return submission
        return self.get_job_status(
            submission_id=submission_id,
            provider_job_id=int(provider_job_id),
        )

    def get_submission_status(self, *, submission_id: int) -> dict[str, Any]:
        """Resolve a provider job ID without querying that job yet."""
        try:
            submission = self._get_unmetered(f"/api/submissions/{submission_id}")
        except ApiClientError as exc:
            if exc.status_code == 404:
                raise AstrometryProviderWorkNotFound(
                    resource="submission",
                    provider_id=submission_id,
                ) from exc
            raise
        jobs = [job for job in submission.get("jobs") or [] if job is not None]
        if not jobs:
            return {
                "provider": "astrometry.net",
                "status": "WAITING",
                "submission_id": submission_id,
                "provider_job_id": None,
            }

        provider_job_id = int(jobs[0])
        return {
            "provider": "astrometry.net",
            "status": "PROCESSING",
            "submission_id": submission_id,
            "provider_job_id": provider_job_id,
        }

    def get_job_status(
        self,
        *,
        submission_id: int | None,
        provider_job_id: int,
    ) -> dict[str, Any]:
        """Query an already resolved provider job and hydrate calibration."""
        try:
            provider_job = self._get_unmetered(f"/api/jobs/{provider_job_id}")
        except ApiClientError as exc:
            if exc.status_code == 404:
                raise AstrometryProviderWorkNotFound(
                    resource="job",
                    provider_id=provider_job_id,
                ) from exc
            raise
        provider_status = str(provider_job.get("status") or "").lower()
        if provider_status == "failure":
            return {
                "provider": "astrometry.net",
                "status": "FAILED",
                "submission_id": submission_id,
                "provider_job_id": provider_job_id,
            }
        if provider_status != "success":
            return {
                "provider": "astrometry.net",
                "status": "PROCESSING",
                "submission_id": submission_id,
                "provider_job_id": provider_job_id,
            }

        calibration = self._get_unmetered(
            f"/api/jobs/{provider_job_id}/calibration/"
        )
        radius = self._number(calibration.get("radius"))
        return {
            "provider": "astrometry.net",
            "status": "COMPLETED",
            "submission_id": submission_id,
            "provider_job_id": provider_job_id,
            "ra": self._number(calibration.get("ra")),
            "dec": self._number(calibration.get("dec")),
            "rotation": self._number(calibration.get("orientation")),
            "pixel_scale": self._number(calibration.get("pixscale")),
            "field_width": radius * 2 if radius is not None else None,
            "field_height": radius * 2 if radius is not None else None,
            "parity": self._number(calibration.get("parity")),
        }

    def _get_unmetered(self, path: str) -> dict[str, Any]:
        """Read public provider job state without consuming a submit unit."""
        url = self._build_url(path)
        try:
            response = self.session.request(method="GET", url=url, timeout=self.timeout)
        except requests.Timeout as exc:
            raise ApiClientError(
                "Astrometry provider timed out",
                code=ExternalApiErrorCode.PROVIDER_TIMEOUT,
            ) from exc
        except requests.RequestException as exc:
            raise ApiClientError("Astrometry provider request failed") from exc
        return self._parse_response(response)

    @staticmethod
    def _number(value: Any) -> float | None:
        return float(value) if value is not None else None
