from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
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
        client_record_id = (
            str(payload.client_record_id) if payload.client_record_id is not None else None
        )
        if client_record_id is not None:
            existing = self.repository.get_by_client_record_id(client_record_id)
            if existing is not None:
                return existing

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

        values = payload.model_dump(exclude={"client_record_id"})
        record = ObservationRecord(
            service_name=self.SERVICE_NAME,
            client_record_id=client_record_id,
            **values,
        )
        try:
            return self.repository.create(record)
        except IntegrityError:
            self.db.rollback()
            if client_record_id is not None:
                existing = self.repository.get_by_client_record_id(client_record_id)
                if existing is not None:
                    return existing
            if payload.representative:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "REPRESENTATIVE_CONFLICT",
                        "catalog_object_id": payload.catalog_object_id,
                    },
                )
            raise

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
            self._raise_revision_conflict(record, payload.revision)

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

        try:
            updated = self.repository.update_if_revision(
                record_id,
                revision=payload.revision,
                values=values,
            )
        except IntegrityError:
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "REPRESENTATIVE_CONFLICT",
                    "catalog_object_id": catalog_object_id,
                },
            )
        if updated is None:
            current = self.repository.get(record_id, include_deleted=True)
            if current is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Record not found",
                )
            self._raise_revision_conflict(current, payload.revision)
        return updated

    def soft_delete(self, record_id: str) -> ObservationRecord:
        record = self.repository.get(record_id, include_deleted=True)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")
        if record.deleted_at is not None:
            return record
        return self.repository.soft_delete(record)

    @staticmethod
    def _raise_revision_conflict(
        record: ObservationRecord,
        expected_revision: int,
    ) -> None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "REVISION_CONFLICT",
                "record_id": record.id,
                "expected_revision": expected_revision,
                "current_revision": record.revision,
            },
        )

    def _require_file(self, file_id: int) -> CommonFile:
        file = self.db.get(CommonFile, file_id)
        if file is None or file.deleted:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="File not found")
        return file
