"""Google Vision API Client."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from google.cloud import vision
from google.oauth2 import service_account
from sqlalchemy.orm import Session

from app.common.config import settings
from app.common.repositories.api_usage_repository import (
    ApiName,
    ApiProvider,
    ApiUsageLimitExceeded,
    ApiUsageRepository,
)
from app.common.services.api_clients.base_client import (
    ApiClientError,
    BaseClient,
)


@dataclass(frozen=True)
class VisionLabel:
    """Vision Label Detection 결과."""

    name: str
    confidence: float


class VisionClient(BaseClient):
    """
    Google Cloud Vision API Client.

    Service Account JSON(GOOGLE_VISION_CREDENTIAL)으로 인증하고
    Label Detection을 수행한다.
    """

    def __init__(
        self,
        *,
        credential_path: str | None = None,
        db: Session | None = None,
        client: vision.ImageAnnotatorClient | None = None,
    ) -> None:
        super().__init__(
            base_url="https://vision.googleapis.com",
            db=db,
            provider=ApiProvider.GOOGLE,
            api_name=ApiName.VISION,
        )
        self.credential_path = (
            credential_path
            if credential_path is not None
            else settings.GOOGLE_VISION_CREDENTIAL
        )
        self._client = client

    def analyze(
        self,
        *,
        image_path: str,
        content: bytes | None = None,
    ) -> list[VisionLabel]:
        """
        이미지 Label Detection을 수행한다.

        Args:
            image_path: 분석 대상 이미지 경로
            content: 이미 읽은 이미지 바이트(있으면 재읽기 생략)

        Returns:
            list[VisionLabel]: name / confidence(0~100) 목록
        """
        path = Path(image_path)
        if not path.is_file():
            raise ApiClientError(f"Vision image file not found: {image_path}")

        if content is None:
            content = path.read_bytes()
        image = vision.Image(content=content)

        client = self._get_client()
        if self.db is None:
            raise ApiClientError("Vision usage reservation database is required")
        if not ApiUsageRepository(self.db).reserve_usage(
            provider=ApiProvider.GOOGLE,
            api_name=ApiName.VISION,
            units=1,
        ):
            raise ApiUsageLimitExceeded("VISION monthly safe limit reached")

        try:
            response = client.label_detection(image=image)
        except Exception as exc:
            self.logger.exception(
                "VisionClient.analyze failed image_path=%s",
                image_path,
            )
            raise ApiClientError(f"Vision API request failed: {exc}") from exc

        if response.error.message:
            raise ApiClientError(
                f"Vision API responded with error: {response.error.message}"
            )

        labels = [
            VisionLabel(
                name=annotation.description,
                confidence=round(float(annotation.score) * 100, 2),
            )
            for annotation in response.label_annotations
            if annotation.description
        ]

        self.logger.info(
            "VisionClient.analyze success image_path=%s labels=%s",
            image_path,
            len(labels),
        )
        return labels

    def _get_client(self) -> vision.ImageAnnotatorClient:
        """Service Account 인증 기반 Vision Client를 반환한다."""
        if self._client is not None:
            return self._client

        if not self.credential_path:
            raise ApiClientError("GOOGLE_VISION_CREDENTIAL is not configured")

        credential_file = Path(self.credential_path)
        if not credential_file.is_file():
            raise ApiClientError(
                f"GOOGLE_VISION_CREDENTIAL file not found: {self.credential_path}"
            )

        credentials = service_account.Credentials.from_service_account_file(
            str(credential_file)
        )
        self._client = vision.ImageAnnotatorClient(credentials=credentials)
        return self._client
