"""Persistent Plate Solve queue API with legacy token lookup compatibility."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.repositories.file_service_repository import FileServiceRepository
from app.common.security.crypto import decrypt_value
from app.common.services.api_clients.astrometry import AstrometryClient
from app.common.services.api_clients.base_client import (
    ApiClientError,
    ExternalApiErrorCode,
)
from app.astrojournal.services.reset_guard import acquire_astrojournal_reset_lock
from app.astrojournal.services.plate_solve_queue_service import PlateSolveQueueService


class PlateSolveService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def submit(self, *, common_file_id: int) -> dict[str, object]:
        common_file = self._get_astro_file(common_file_id)
        queue = PlateSolveQueueService(self.db)
        queued = queue.get_for_file(common_file.id)
        if queued is not None:
            return queue.response(queued)
        if not common_file.original_path:
            raise ApiClientError(
                "Common file has no original media path",
                code=ExternalApiErrorCode.INVALID_REQUEST,
            )
        job, _ = queue.enqueue(
            common_file_id=common_file.id,
            observation_record_id=None,
        )
        return queue.response(job)

    def get(self, *, job_id: str) -> dict[str, object]:
        queued = PlateSolveQueueService(self.db).get_optional(job_id)
        if queued is not None:
            return PlateSolveQueueService(self.db).response(queued)

        payload = self._decode_job(job_id)
        common_file = self._get_astro_file(int(payload["common_file_id"]))
        common_file_id = int(common_file.id)
        width = common_file.width
        height = common_file.height
        # Legacy provider polling remains supported, but its validation read
        # transaction must not stay open during the external HTTP request.
        self.db.rollback()
        provider = AstrometryClient(api_key=None, db=None).get_status(
            submission_id=int(payload["submission_id"])
        )
        result = None
        if provider["status"] == "COMPLETED":
            pixel_scale = provider.get("pixel_scale")
            field_width = provider.get("field_width")
            field_height = provider.get("field_height")
            if pixel_scale is not None and width:
                field_width = float(pixel_scale) * width / 3600.0
            if pixel_scale is not None and height:
                field_height = float(pixel_scale) * height / 3600.0
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
            "common_file_id": common_file_id,
            "provider": "astrometry.net",
            "result": result,
            "provider_metadata": {
                "submission_id": provider.get("submission_id"),
                "provider_job_id": provider.get("provider_job_id"),
            },
        }

    def summary(self) -> dict[str, int]:
        return PlateSolveQueueService(self.db).summary()

    def retry(self, *, job_id: str) -> dict[str, object]:
        queue = PlateSolveQueueService(self.db)
        return queue.response(queue.retry(job_id))

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
