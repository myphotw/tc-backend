from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.astrojournal.models.multi_night_framing_reference import (
    AstroMultiNightFramingReference,
)
from app.astrojournal.repositories.equipment_repository import EquipmentRepository
from app.astrojournal.repositories.multi_night_framing_reference_repository import (
    MultiNightFramingReferenceRepository,
)
from app.astrojournal.repositories.observation_site_repository import (
    ObservationSiteRepository,
)
from app.astrojournal.schemas.multi_night_framing_reference import (
    MultiNightFramingReferenceCreate,
    MultiNightFramingReferenceDeleteResponse,
    MultiNightFramingReferenceResponse,
    MultiNightFramingReferenceUpdate,
)
from app.common.repositories.change_event_repository import (
    ChangeEventRepository,
    ChangeOperation,
)


class MultiNightFramingReferenceService:
    SERVICE_NAME = "AstroJournal"
    RESOURCE_TYPE = "MultiNightFramingReference"

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = MultiNightFramingReferenceRepository(db)
        self.sites = ObservationSiteRepository(db)
        self.equipment = EquipmentRepository(db)
        self.changes = ChangeEventRepository(db)

    def create(
        self,
        payload: MultiNightFramingReferenceCreate,
    ) -> MultiNightFramingReferenceResponse:
        reference_id = str(payload.id)
        existing = self.repository.get(reference_id, include_deleted=True)
        if existing is not None:
            if existing.deleted_at is not None:
                self._deleted_id_conflict(reference_id)
            return MultiNightFramingReferenceResponse.model_validate(existing)

        try:
            self._require_site(payload.site_id)
            self._require_equipment(payload.equipment_id)
            duplicate = self.repository.find_active_identity(
                catalog_object_id=payload.catalog_object_id,
                equipment_id=str(payload.equipment_id),
            )
            if duplicate is not None:
                self._logical_conflict(duplicate)

            reference = AstroMultiNightFramingReference(
                id=reference_id,
                catalog_object_id=payload.catalog_object_id,
                reference_captured_at=payload.reference_captured_at,
                site_id=str(payload.site_id),
                equipment_id=str(payload.equipment_id),
                reference_hour_angle_deg=payload.reference_hour_angle_deg,
                reference_parallactic_angle_deg=(
                    payload.reference_parallactic_angle_deg
                ),
                reference_branch=payload.reference_branch,
            )
            self.repository.create(reference)
            self._append_change(reference, ChangeOperation.CREATE)
            self.db.commit()
            self.db.refresh(reference)
            return MultiNightFramingReferenceResponse.model_validate(reference)
        except IntegrityError:
            self.db.rollback()
            same_id = self.repository.get(reference_id, include_deleted=True)
            if same_id is not None and same_id.deleted_at is None:
                return MultiNightFramingReferenceResponse.model_validate(same_id)
            duplicate = self.repository.find_active_identity(
                catalog_object_id=payload.catalog_object_id,
                equipment_id=str(payload.equipment_id),
            )
            if duplicate is not None:
                self._logical_conflict(duplicate)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "MULTI_NIGHT_REFERENCE_AGGREGATE_CONFLICT",
                    "reference_id": reference_id,
                },
            )
        except Exception:
            self.db.rollback()
            raise

    def get(self, reference_id: str) -> MultiNightFramingReferenceResponse:
        reference = self.repository.get(reference_id)
        if reference is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Multi-night framing reference not found",
            )
        return MultiNightFramingReferenceResponse.model_validate(reference)

    def list(
        self,
        *,
        catalog_object_id: str | None = None,
        equipment_id: UUID | None = None,
    ) -> list[MultiNightFramingReferenceResponse]:
        normalized_catalog_id = (
            self._normalize_catalog_filter(catalog_object_id)
            if catalog_object_id is not None
            else None
        )
        return [
            MultiNightFramingReferenceResponse.model_validate(reference)
            for reference in self.repository.list(
                catalog_object_id=normalized_catalog_id,
                equipment_id=str(equipment_id) if equipment_id is not None else None,
            )
        ]

    def update(
        self,
        reference_id: str,
        payload: MultiNightFramingReferenceUpdate,
    ) -> MultiNightFramingReferenceResponse:
        conflict_catalog_object_id: str | None = None
        conflict_equipment_id: str | None = None
        try:
            reference = self.repository.get(reference_id, lock=True)
            if reference is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Multi-night framing reference not found",
                )
            if reference.revision != payload.expected_revision:
                self._revision_conflict(reference, payload.expected_revision)

            values = payload.model_dump(
                exclude={"expected_revision"},
                exclude_unset=True,
            )
            if "site_id" in values:
                self._require_site(values["site_id"])
                values["site_id"] = str(values["site_id"])
            if "equipment_id" in values:
                self._require_equipment(values["equipment_id"])
                values["equipment_id"] = str(values["equipment_id"])

            equipment_id = str(values.get("equipment_id", reference.equipment_id))
            conflict_catalog_object_id = reference.catalog_object_id
            conflict_equipment_id = equipment_id
            duplicate = self.repository.find_active_identity(
                catalog_object_id=reference.catalog_object_id,
                equipment_id=equipment_id,
                exclude_reference_id=reference.id,
            )
            if duplicate is not None:
                self._logical_conflict(duplicate)

            for name, value in values.items():
                setattr(reference, name, value)
            reference.revision += 1
            reference.updated_at = datetime.now(timezone.utc)
            self._append_change(reference, ChangeOperation.UPDATE)
            self.db.commit()
            self.db.refresh(reference)
            return MultiNightFramingReferenceResponse.model_validate(reference)
        except IntegrityError:
            self.db.rollback()
            if (
                conflict_catalog_object_id is not None
                and conflict_equipment_id is not None
            ):
                duplicate = self.repository.find_active_identity(
                    catalog_object_id=conflict_catalog_object_id,
                    equipment_id=conflict_equipment_id,
                    exclude_reference_id=reference_id,
                )
                if duplicate is not None:
                    self._logical_conflict(duplicate)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "MULTI_NIGHT_REFERENCE_AGGREGATE_CONFLICT",
                    "reference_id": reference_id,
                },
            )
        except Exception:
            self.db.rollback()
            raise

    def soft_delete(
        self,
        reference_id: str,
        *,
        expected_revision: int,
    ) -> MultiNightFramingReferenceDeleteResponse:
        try:
            reference = self.repository.get(reference_id, lock=True)
            if reference is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Multi-night framing reference not found",
                )
            if reference.revision != expected_revision:
                self._revision_conflict(reference, expected_revision)

            changed_at = datetime.now(timezone.utc)
            reference.deleted_at = changed_at
            reference.updated_at = changed_at
            reference.revision += 1
            self._append_change(reference, ChangeOperation.DELETE, tombstone=True)
            result = MultiNightFramingReferenceDeleteResponse(
                reference_id=reference.id,
                deleted=True,
                revision=reference.revision,
                deleted_at=reference.deleted_at,
            )
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def _require_site(self, site_id: UUID) -> None:
        if self.sites.get(str(site_id)) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Active observation site not found",
            )

    def _require_equipment(self, equipment_id: UUID) -> None:
        equipment = self.equipment.get(str(equipment_id))
        if equipment is None or not equipment.is_active:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Active equipment not found",
            )

    def _append_change(
        self,
        reference: AstroMultiNightFramingReference,
        operation: str,
        *,
        tombstone: bool = False,
    ) -> None:
        self.changes.append(
            service_name=self.SERVICE_NAME,
            resource_type=self.RESOURCE_TYPE,
            resource_id=reference.id,
            operation=operation,
            revision=reference.revision,
            tombstone=tombstone,
        )

    @staticmethod
    def _normalize_catalog_filter(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="catalog_object_id cannot be blank",
            )
        return normalized

    @staticmethod
    def _revision_conflict(
        reference: AstroMultiNightFramingReference,
        expected_revision: int,
    ) -> None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "REVISION_CONFLICT",
                "reference_id": reference.id,
                "expected_revision": expected_revision,
                "current_revision": reference.revision,
            },
        )

    @staticmethod
    def _logical_conflict(reference: AstroMultiNightFramingReference) -> None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "REFERENCE_ALREADY_EXISTS",
                "reference_id": reference.id,
                "catalog_object_id": reference.catalog_object_id,
                "equipment_id": reference.equipment_id,
            },
        )

    @staticmethod
    def _deleted_id_conflict(reference_id: str) -> None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RESOURCE_TOMBSTONED",
                "reference_id": reference_id,
            },
        )
