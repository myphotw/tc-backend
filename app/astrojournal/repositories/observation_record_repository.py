from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.astrojournal.models.observation_record import ObservationRecord


class ObservationRecordRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, record: ObservationRecord) -> ObservationRecord:
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def get(self, record_id: str, *, include_deleted: bool = False) -> ObservationRecord | None:
        query = self.db.query(ObservationRecord).filter(ObservationRecord.id == record_id)
        if not include_deleted:
            query = query.filter(ObservationRecord.deleted_at.is_(None))
        return query.first()

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

    def clear_representative(self, catalog_object_id: str, *, except_id: str | None = None) -> None:
        query = (
            self.db.query(ObservationRecord)
            .filter(ObservationRecord.catalog_object_id == catalog_object_id)
            .filter(ObservationRecord.deleted_at.is_(None))
        )
        if except_id is not None:
            query = query.filter(ObservationRecord.id != except_id)
        query.update({ObservationRecord.representative: False}, synchronize_session=False)

    def update_if_revision(
        self,
        record_id: str,
        *,
        revision: int,
        values: dict[str, object],
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
        self.db.commit()
        return self.get(record_id)

    def soft_delete(self, record: ObservationRecord) -> ObservationRecord:
        record.deleted_at = datetime.now(timezone.utc)
        record.revision += 1
        self.db.commit()
        self.db.refresh(record)
        return record
