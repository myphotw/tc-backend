"""Stateless Backend job facade over Astrometry.net submission identifiers."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.repositories.file_service_repository import FileServiceRepository
from app.common.security.crypto import decrypt_value, encrypt_value
from app.common.services.api_clients.astrometry import AstrometryClient
from app.common.services.api_clients.base_client import (
    ApiClientError,
    ExternalApiErrorCode,
)
from app.common.services.key_resolver import ExternalServiceName, KeyResolver
from app.common.services.storage_service import StorageService
from app.astrojournal.services.reset_guard import acquire_astrojournal_reset_lock


class PlateSolveService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def submit(self, *, common_file_id: int) -> dict[str, object]:
        common_file = self._get_astro_file(common_file_id)
        if not common_file.original_path:
            raise ApiClientError(
                "Common file has no original media path",
                code=ExternalApiErrorCode.INVALID_REQUEST,
            )
        image_path = StorageService().resolve_storage_path(common_file.original_path)
        api_key = KeyResolver(self.db).resolve(ExternalServiceName.ASTROMETRY)
        if not api_key:
            raise ApiClientError(
                "ASTROMETRY is not configured",
                code=ExternalApiErrorCode.API_KEY_NOT_CONFIGURED,
            )
        submitted = AstrometryClient(api_key=api_key, db=self.db).submit(
            image_path=str(image_path)
        )
        submission_id = int(submitted["submission_id"])
        token = encrypt_value(
            json.dumps(
                {
                    "v": 1,
                    "submission_id": submission_id,
                    "common_file_id": common_file.id,
                },
                separators=(",", ":"),
            )
        )
        return {
            "job_id": token,
            "status": "WAITING",
            "common_file_id": common_file.id,
            "provider": "astrometry.net",
            "result": None,
            "provider_metadata": {"submission_id": submission_id},
        }

    def get(self, *, job_id: str) -> dict[str, object]:
        payload = self._decode_job(job_id)
        common_file = self._get_astro_file(int(payload["common_file_id"]))
        provider = AstrometryClient(api_key=None, db=None).get_status(
            submission_id=int(payload["submission_id"])
        )
        result = None
        if provider["status"] == "COMPLETED":
            pixel_scale = provider.get("pixel_scale")
            field_width = provider.get("field_width")
            field_height = provider.get("field_height")
            if pixel_scale is not None and common_file.width:
                field_width = float(pixel_scale) * common_file.width / 3600.0
            if pixel_scale is not None and common_file.height:
                field_height = float(pixel_scale) * common_file.height / 3600.0
            result = {
                "ra": provider.get("ra"),
                "dec": provider.get("dec"),
                "rotation": provider.get("rotation"),
                "pixel_scale": pixel_scale,
                "field_width": field_width,
                "field_height": field_height,
                "parity": provider.get("parity"),
            }
        return {
            "job_id": job_id,
            "status": provider["status"],
            "common_file_id": common_file.id,
            "provider": "astrometry.net",
            "result": result,
            "provider_metadata": {
                "submission_id": provider.get("submission_id"),
                "provider_job_id": provider.get("provider_job_id"),
            },
        }

    def _get_astro_file(self, common_file_id: int) -> CommonFile:
        acquire_astrojournal_reset_lock(self.db, exclusive=False)
        common_file = (
            self.db.query(CommonFile)
            .filter(CommonFile.id == common_file_id)
            .filter(CommonFile.deleted.is_(False))
            .first()
        )
        if common_file is None or FileServiceRepository(self.db).get(
            file_id=common_file_id,
            service_name="AstroJournal",
        ) is None:
            raise ApiClientError(
                "AstroJournal common file was not found",
                code=ExternalApiErrorCode.INVALID_REQUEST,
            )
        return common_file

    @staticmethod
    def _decode_job(job_id: str) -> dict[str, int]:
        try:
            payload = json.loads(decrypt_value(job_id))
            if payload.get("v") != 1:
                raise ValueError("unsupported token version")
            return {
                "submission_id": int(payload["submission_id"]),
                "common_file_id": int(payload["common_file_id"]),
            }
        except Exception as exc:
            raise ApiClientError(
                "Plate solve job_id is invalid",
                code=ExternalApiErrorCode.INVALID_REQUEST,
            ) from exc
