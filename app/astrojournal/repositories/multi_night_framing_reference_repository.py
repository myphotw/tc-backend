from __future__ import annotations

from sqlalchemy.orm import Session

from app.astrojournal.models.multi_night_framing_reference import (
    AstroMultiNightFramingReference,
)


class MultiNightFramingReferenceRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        reference: AstroMultiNightFramingReference,
    ) -> AstroMultiNightFramingReference:
        self.db.add(reference)
        self.db.flush()
        return reference

    def get(
        self,
        reference_id: str,
        *,
        include_deleted: bool = False,
        lock: bool = False,
    ) -> AstroMultiNightFramingReference | None:
        query = self.db.query(AstroMultiNightFramingReference).filter(
            AstroMultiNightFramingReference.id == reference_id
        )
        if not include_deleted:
            query = query.filter(AstroMultiNightFramingReference.deleted_at.is_(None))
        if lock:
            query = query.with_for_update()
        return query.first()

    def list(
        self,
        *,
        catalog_object_id: str | None = None,
        equipment_id: str | None = None,
    ) -> list[AstroMultiNightFramingReference]:
        query = self.db.query(AstroMultiNightFramingReference).filter(
            AstroMultiNightFramingReference.deleted_at.is_(None)
        )
        if catalog_object_id is not None:
            query = query.filter(
                AstroMultiNightFramingReference.catalog_object_id
                == catalog_object_id
            )
        if equipment_id is not None:
            query = query.filter(
                AstroMultiNightFramingReference.equipment_id == equipment_id
            )
        return (
            query.order_by(
                AstroMultiNightFramingReference.reference_captured_at.desc(),
                AstroMultiNightFramingReference.id.asc(),
            )
            .all()
        )

    def find_active_identity(
        self,
        *,
        catalog_object_id: str,
        equipment_id: str,
        exclude_reference_id: str | None = None,
    ) -> AstroMultiNightFramingReference | None:
        query = self.db.query(AstroMultiNightFramingReference).filter(
            AstroMultiNightFramingReference.deleted_at.is_(None),
            AstroMultiNightFramingReference.catalog_object_id
            == catalog_object_id,
            AstroMultiNightFramingReference.equipment_id == equipment_id,
        )
        if exclude_reference_id is not None:
            query = query.filter(
                AstroMultiNightFramingReference.id != exclude_reference_id
            )
        return query.first()
