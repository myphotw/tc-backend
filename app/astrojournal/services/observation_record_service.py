from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.repositories.observation_record_repository import (
    ObservationRecordRepository,
)
from app.astrojournal.schemas.observation_record import (
    ObservationRecordCreate,
    ObservationRecordUpdate,
)
from app.common.models.file import CommonFile
from app.common.repositories.file_service_repository import FileServiceRepository


class ObservationRecordService:
    SERVICE_NAME = "AstroJournal"

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = ObservationRecordRepository(db)

    def create(self, payload: ObservationRecordCreate) -> ObservationRecord:
        self._require_file(payload.file_id)
        if payload.representative and not payload.catalog_object_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="representative requires catalog_object_id",
            )

        FileServiceRepository(self.db).ensure_link(
            file_id=payload.file_id,
            service_name=self.SERVICE_NAME,
        )
        if payload.representative:
            self.repository.clear_representative(payload.catalog_object_id)

        record = ObservationRecord(
            service_name=self.SERVICE_NAME,
            **payload.model_dump(),
        )
        return self.repository.create(record)

    def get(self, record_id: str) -> ObservationRecord:
        record = self.repository.get(record_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
        return record

    def list(
        self,
        *,
        catalog_object_id: str | None = None,
        favorite: bool | None = None,
        representative: bool | None = None,
    ) -> list[ObservationRecord]:
        return self.repository.list(
            catalog_object_id=catalog_object_id,
            favorite=favorite,
            representative=representative,
        )

    def update(self, record_id: str, payload: ObservationRecordUpdate) -> ObservationRecord:
        record = self.get(record_id)
        if record.revision != payload.revision:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Revision conflict")

        values = payload.model_dump(exclude={"revision"}, exclude_unset=True)
        catalog_object_id = values.get("catalog_object_id", record.catalog_object_id)
        representative = values.get("representative", record.representative)
        if representative and not catalog_object_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="representative requires catalog_object_id",
            )
        if representative:
            self.repository.clear_representative(catalog_object_id, except_id=record.id)

        updated = self.repository.update_if_revision(
            record_id,
            revision=payload.revision,
            values=values,
        )
        if updated is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Revision conflict")
        return updated

    def soft_delete(self, record_id: str) -> ObservationRecord:
        return self.repository.soft_delete(self.get(record_id))

    def _require_file(self, file_id: int) -> CommonFile:
        file = self.db.get(CommonFile, file_id)
        if file is None or file.deleted:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="File not found")
        return file
