from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.astrojournal.models.equipment import AstroEquipment, AstroEquipmentEyepiece
from app.astrojournal.repositories.equipment_repository import EquipmentRepository
from app.astrojournal.repositories.observation_site_repository import (
    ObservationSiteRepository,
)
from app.astrojournal.schemas.equipment import (
    EquipmentCreate,
    EquipmentDeleteResponse,
    EquipmentResponse,
    EquipmentUpdate,
    ExposureCapability,
    EyepieceResponse,
    EyepieceWrite,
)
from app.common.repositories.change_event_repository import (
    ChangeEventRepository,
    ChangeOperation,
)


class EquipmentService:
    SERVICE_NAME = "AstroJournal"
    RESOURCE_TYPE = "Equipment"
    OBSERVATION_SITE_RESOURCE_TYPE = "ObservationSite"

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = EquipmentRepository(db)
        self.sites = ObservationSiteRepository(db)
        self.changes = ChangeEventRepository(db)

    def create(self, payload: EquipmentCreate) -> EquipmentResponse:
        equipment_id = str(payload.id)
        existing = self.repository.get(equipment_id, include_deleted=True)
        if existing is not None:
            if existing.deleted_at is not None:
                self._deleted_id_conflict(equipment_id)
            return self._response(existing)

        equipment = AstroEquipment(
            id=equipment_id,
            **self._parent_values(payload),
        )
        try:
            self.repository.create(equipment)
            self.repository.replace_eyepieces(
                equipment.id,
                self._eyepiece_values(payload.eyepieces),
            )
            self.repository.replace_capability(
                equipment.id,
                "AZ",
                self._capability_value(payload.az_exposure_capability),
            )
            self.repository.replace_capability(
                equipment.id,
                "EQ",
                self._capability_value(payload.eq_exposure_capability),
            )
            self._append_change(equipment, ChangeOperation.CREATE)
            self.db.commit()
            self.db.refresh(equipment)
            return self._response(equipment)
        except IntegrityError:
            self.db.rollback()
            existing = self.repository.get(equipment_id, include_deleted=True)
            if existing is not None and existing.deleted_at is None:
                return self._response(existing)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "EQUIPMENT_AGGREGATE_CONFLICT"},
            )
        except Exception:
            self.db.rollback()
            raise

    def get(self, equipment_id: str) -> EquipmentResponse:
        equipment = self.repository.get(equipment_id)
        if equipment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Equipment not found",
            )
        return self._response(equipment)

    def list(self) -> list[EquipmentResponse]:
        equipment = self.repository.list()
        equipment_ids = [item.id for item in equipment]
        eyepieces = self.repository.eyepieces(equipment_ids)
        capabilities = self.repository.capabilities(equipment_ids)
        return [
            self._response(
                item,
                eyepieces=eyepieces.get(item.id, []),
                capabilities=capabilities.get(item.id, {}),
            )
            for item in equipment
        ]

    def update(
        self,
        equipment_id: str,
        payload: EquipmentUpdate,
    ) -> EquipmentResponse:
        try:
            equipment = self.repository.get(equipment_id, lock=True)
            if equipment is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Equipment not found",
                )
            if equipment.revision != payload.expected_revision:
                self._revision_conflict(equipment, payload.expected_revision)

            current = self._response(equipment)
            values = current.model_dump(
                exclude={"revision", "created_at", "updated_at", "deleted_at"}
            )
            values.update(
                payload.model_dump(
                    exclude={"expected_revision"},
                    exclude_unset=True,
                )
            )
            candidate = EquipmentCreate.model_validate(values)
            for name, value in self._parent_values(candidate).items():
                setattr(equipment, name, value)
            if "eyepieces" in payload.model_fields_set:
                self.repository.replace_eyepieces(
                    equipment.id,
                    self._eyepiece_values(candidate.eyepieces),
                )
            if "az_exposure_capability" in payload.model_fields_set:
                self.repository.replace_capability(
                    equipment.id,
                    "AZ",
                    self._capability_value(candidate.az_exposure_capability),
                )
            if "eq_exposure_capability" in payload.model_fields_set:
                self.repository.replace_capability(
                    equipment.id,
                    "EQ",
                    self._capability_value(candidate.eq_exposure_capability),
                )
            equipment.revision += 1
            equipment.updated_at = datetime.now(timezone.utc)
            self._append_change(equipment, ChangeOperation.UPDATE)
            self.db.commit()
            self.db.refresh(equipment)
            return self._response(equipment)
        except IntegrityError:
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "EQUIPMENT_AGGREGATE_CONFLICT"},
            )
        except Exception:
            self.db.rollback()
            raise

    def soft_delete(
        self,
        equipment_id: str,
        *,
        expected_revision: int,
    ) -> EquipmentDeleteResponse:
        try:
            equipment = self.repository.get(equipment_id, lock=True)
            if equipment is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Equipment not found",
                )
            if equipment.revision != expected_revision:
                self._revision_conflict(equipment, expected_revision)

            changed_at = datetime.now(timezone.utc)
            for site in self.sites.active_sites_using_equipment(equipment.id):
                site.default_equipment_id = None
                site.revision += 1
                site.updated_at = changed_at
                self.changes.append(
                    service_name=self.SERVICE_NAME,
                    resource_type=self.OBSERVATION_SITE_RESOURCE_TYPE,
                    resource_id=site.id,
                    operation=ChangeOperation.UPDATE,
                    revision=site.revision,
                )
            self.repository.delete_children(equipment.id)
            equipment.deleted_at = changed_at
            equipment.updated_at = changed_at
            equipment.revision += 1
            self._append_change(equipment, ChangeOperation.DELETE, tombstone=True)
            result = EquipmentDeleteResponse(
                equipment_id=equipment.id,
                deleted=True,
                revision=equipment.revision,
                deleted_at=equipment.deleted_at,
            )
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def _response(
        self,
        equipment: AstroEquipment,
        *,
        eyepieces: list[AstroEquipmentEyepiece] | None = None,
        capabilities: dict[str, dict[str, object]] | None = None,
    ) -> EquipmentResponse:
        if eyepieces is None:
            eyepieces = self.repository.eyepieces([equipment.id]).get(
                equipment.id,
                [],
            )
        if capabilities is None:
            capabilities = self.repository.capabilities([equipment.id]).get(
                equipment.id,
                {},
            )
        return EquipmentResponse.model_validate(
            {
                **{
                    column.name: getattr(equipment, column.name)
                    for column in equipment.__table__.columns
                },
                "eyepieces": [
                    EyepieceResponse.model_validate(item, from_attributes=True)
                    for item in eyepieces
                ],
                "az_exposure_capability": capabilities.get("AZ"),
                "eq_exposure_capability": capabilities.get("EQ"),
            }
        )

    @staticmethod
    def _parent_values(payload: EquipmentCreate) -> dict[str, object]:
        return payload.model_dump(
            exclude={
                "id",
                "eyepieces",
                "az_exposure_capability",
                "eq_exposure_capability",
            }
        )

    @staticmethod
    def _eyepiece_values(items: list[EyepieceWrite]) -> list[dict[str, object]]:
        return [
            {
                **item.model_dump(exclude={"equipment_id"}),
                "id": str(item.id),
            }
            for item in items
        ]

    @staticmethod
    def _capability_value(
        value: ExposureCapability | None,
    ) -> dict[str, object] | None:
        return value.model_dump() if value is not None else None

    def _append_change(
        self,
        equipment: AstroEquipment,
        operation: str,
        *,
        tombstone: bool = False,
    ) -> None:
        self.changes.append(
            service_name=self.SERVICE_NAME,
            resource_type=self.RESOURCE_TYPE,
            resource_id=equipment.id,
            operation=operation,
            revision=equipment.revision,
            tombstone=tombstone,
        )

    @staticmethod
    def _revision_conflict(
        equipment: AstroEquipment,
        expected_revision: int,
    ) -> None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "REVISION_CONFLICT",
                "equipment_id": equipment.id,
                "expected_revision": expected_revision,
                "current_revision": equipment.revision,
            },
        )

    @staticmethod
    def _deleted_id_conflict(equipment_id: str) -> None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "RESOURCE_TOMBSTONED",
                "equipment_id": equipment_id,
            },
        )
