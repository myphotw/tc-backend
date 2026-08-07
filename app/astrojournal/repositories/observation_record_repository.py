from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.astrojournal.models.observation_record import ObservationRecord


class ObservationRecordRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        record: ObservationRecord,
        *,
        commit: bool = True,
    ) -> ObservationRecord:
        self.db.add(record)
        self.db.flush()
        if commit:
            self.db.commit()
            self.db.refresh(record)
        return record

    def get(self, record_id: str, *, include_deleted: bool = False) -> ObservationRecord | None:
        query = self.db.query(ObservationRecord).filter(ObservationRecord.id == record_id)
        if not include_deleted:
            query = query.filter(ObservationRecord.deleted_at.is_(None))
        return query.first()

    def get_by_client_record_id(
        self,
        client_record_id: str,
    ) -> ObservationRecord | None:
        return (
            self.db.query(ObservationRecord)
            .filter(ObservationRecord.service_name == "AstroJournal")
            .filter(ObservationRecord.client_record_id == client_record_id)
            .first()
        )

    def list(
        self,
        *,
        catalog_object_id: str | None = None,
        favorite: bool | None = None,
        representative: bool | None = None,
    ) -> list[ObservationRecord]:
        query = self.db.query(ObservationRecord).filter(ObservationRecord.deleted_at.is_(None))
        if catalog_object_id is not None:
            query = query.filter(ObservationRecord.catalog_object_id == catalog_object_id)
        if favorite is not None:
            query = query.filter(ObservationRecord.favorite.is_(favorite))
        if representative is not None:
            query = query.filter(ObservationRecord.representative.is_(representative))
        return query.order_by(ObservationRecord.captured_at.desc(), ObservationRecord.id.desc()).all()

    def clear_representative(
        self,
        catalog_object_id: str,
        *,
        except_id: str | None = None,
    ) -> list[ObservationRecord]:
        query = (
            self.db.query(ObservationRecord)
            .filter(ObservationRecord.service_name == "AstroJournal")
            .filter(ObservationRecord.catalog_object_id == catalog_object_id)
            .filter(ObservationRecord.deleted_at.is_(None))
            .filter(ObservationRecord.representative.is_(True))
        )
        if except_id is not None:
            query = query.filter(ObservationRecord.id != except_id)
        records = query.with_for_update().all()
        changed_at = datetime.now(timezone.utc)
        for record in records:
            record.representative = False
            record.revision += 1
            record.updated_at = changed_at
        self.db.flush()
        return records

    def update_if_revision(
        self,
        record_id: str,
        *,
        revision: int,
        values: dict[str, object],
        commit: bool = True,
    ) -> ObservationRecord | None:
        values = {
            **values,
            "revision": revision + 1,
            "updated_at": datetime.now(timezone.utc),
        }
        result = self.db.execute(
            update(ObservationRecord)
            .where(ObservationRecord.id == record_id)
            .where(ObservationRecord.deleted_at.is_(None))
            .where(ObservationRecord.revision == revision)
            .values(**values)
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        if commit:
            self.db.commit()
        return self.get(record_id)

    def soft_delete(
        self,
        record: ObservationRecord,
        *,
        commit: bool = True,
    ) -> ObservationRecord:
        record.deleted_at = datetime.now(timezone.utc)
        record.revision += 1
        self.db.flush()
        if commit:
            self.db.commit()
            self.db.refresh(record)
        return record
