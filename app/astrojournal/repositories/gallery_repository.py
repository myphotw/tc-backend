from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.astrojournal.models.observation_record import ObservationRecord
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService


AstroGalleryRow = tuple[ObservationRecord, CommonFile, CommonFileMetadata | None]


class AstroGalleryRepository:
    SERVICE_NAME = "AstroJournal"

    def __init__(self, db: Session) -> None:
        self.db = db

    def list(
        self,
        *,
        page: int,
        page_size: int,
        catalog_object_id: str | None = None,
        favorite: bool | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> tuple[list[AstroGalleryRow], int]:
        query = self._base_query()
        if catalog_object_id is not None:
            query = query.filter(ObservationRecord.catalog_object_id == catalog_object_id)
        if favorite is not None:
            query = query.filter(ObservationRecord.favorite.is_(favorite))
        if date_from is not None:
            query = query.filter(ObservationRecord.captured_at >= date_from)
        if date_to is not None:
            query = query.filter(ObservationRecord.captured_at <= date_to)

        total = query.count()
        rows = (
            query.order_by(
                ObservationRecord.captured_at.desc(),
                ObservationRecord.created_at.desc(),
                ObservationRecord.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return rows, total

    def get(self, record_id: str) -> AstroGalleryRow | None:
        return self._base_query().filter(ObservationRecord.id == record_id).first()

    def _base_query(self):
        return (
            self.db.query(ObservationRecord, CommonFile, CommonFileMetadata)
            .join(CommonFile, CommonFile.id == ObservationRecord.file_id)
            .join(
                CommonFileService,
                (CommonFileService.file_id == CommonFile.id)
                & (CommonFileService.service_name == self.SERVICE_NAME),
            )
            .outerjoin(CommonFileMetadata, CommonFileMetadata.file_id == CommonFile.id)
            .filter(ObservationRecord.service_name == self.SERVICE_NAME)
            .filter(ObservationRecord.deleted_at.is_(None))
            .filter(CommonFile.deleted.is_(False))
        )
