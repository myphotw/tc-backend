from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy.orm import Session

from app.astrojournal.models.equipment import (
    AstroEquipment,
    AstroEquipmentExposureCapability,
    AstroEquipmentExposureValue,
    AstroEquipmentEyepiece,
)


class EquipmentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, equipment: AstroEquipment) -> AstroEquipment:
        self.db.add(equipment)
        self.db.flush()
        return equipment

    def get(
        self,
        equipment_id: str,
        *,
        include_deleted: bool = False,
        lock: bool = False,
    ) -> AstroEquipment | None:
        query = self.db.query(AstroEquipment).filter(
            AstroEquipment.id == equipment_id
        )
        if not include_deleted:
            query = query.filter(AstroEquipment.deleted_at.is_(None))
        if lock:
            query = query.with_for_update()
        return query.first()

    def list(self) -> list[AstroEquipment]:
        return (
            self.db.query(AstroEquipment)
            .filter(AstroEquipment.deleted_at.is_(None))
            .order_by(
                AstroEquipment.sort_order.asc(),
                AstroEquipment.name.asc(),
                AstroEquipment.id.asc(),
            )
            .all()
        )

    def eyepieces(
        self,
        equipment_ids: list[str],
    ) -> dict[str, list[AstroEquipmentEyepiece]]:
        grouped: dict[str, list[AstroEquipmentEyepiece]] = defaultdict(list)
        if not equipment_ids:
            return grouped
        rows = (
            self.db.query(AstroEquipmentEyepiece)
            .filter(AstroEquipmentEyepiece.equipment_id.in_(equipment_ids))
            .order_by(
                AstroEquipmentEyepiece.equipment_id.asc(),
                AstroEquipmentEyepiece.sort_order.asc(),
                AstroEquipmentEyepiece.id.asc(),
            )
            .all()
        )
        for row in rows:
            grouped[row.equipment_id].append(row)
        return grouped

    def capabilities(
        self,
        equipment_ids: list[str],
    ) -> dict[str, dict[str, dict[str, object]]]:
        grouped: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
        if not equipment_ids:
            return grouped
        capabilities = (
            self.db.query(AstroEquipmentExposureCapability)
            .filter(
                AstroEquipmentExposureCapability.equipment_id.in_(equipment_ids)
            )
            .order_by(
                AstroEquipmentExposureCapability.equipment_id.asc(),
                AstroEquipmentExposureCapability.tracking_mode.asc(),
            )
            .all()
        )
        capability_ids = [item.id for item in capabilities]
        values_by_capability: dict[int, list[Decimal]] = defaultdict(list)
        if capability_ids:
            for value in (
                self.db.query(AstroEquipmentExposureValue)
                .filter(
                    AstroEquipmentExposureValue.capability_id.in_(capability_ids)
                )
                .order_by(
                    AstroEquipmentExposureValue.capability_id.asc(),
                    AstroEquipmentExposureValue.sort_order.asc(),
                )
                .all()
            ):
                values_by_capability[value.capability_id].append(value.value_seconds)

        for capability in capabilities:
            if capability.capability_type == "discrete":
                payload: dict[str, object] = {
                    "type": "discrete",
                    "values_seconds": values_by_capability[capability.id],
                }
            else:
                payload = {
                    "type": "range",
                    "min_seconds": capability.min_seconds,
                    "max_seconds": capability.max_seconds,
                    "step_seconds": capability.step_seconds,
                }
            grouped[capability.equipment_id][capability.tracking_mode] = payload
        return grouped

    def replace_eyepieces(
        self,
        equipment_id: str,
        values: list[dict[str, object]],
    ) -> None:
        existing = (
            self.db.query(AstroEquipmentEyepiece)
            .filter(AstroEquipmentEyepiece.equipment_id == equipment_id)
            .all()
        )
        for child in existing:
            self.db.delete(child)
        self.db.flush()
        for item in values:
            child_values = dict(item)
            child_id = str(child_values.pop("id"))
            self.db.add(
                AstroEquipmentEyepiece(
                    id=child_id,
                    equipment_id=equipment_id,
                    **child_values,
                )
            )
        self.db.flush()

    def replace_capability(
        self,
        equipment_id: str,
        tracking_mode: str,
        value: dict[str, object] | None,
    ) -> None:
        capability = (
            self.db.query(AstroEquipmentExposureCapability)
            .filter(
                AstroEquipmentExposureCapability.equipment_id == equipment_id
            )
            .filter(
                AstroEquipmentExposureCapability.tracking_mode == tracking_mode
            )
            .one_or_none()
        )
        if capability is not None:
            existing_values = (
                self.db.query(AstroEquipmentExposureValue)
                .filter(
                    AstroEquipmentExposureValue.capability_id == capability.id
                )
                .all()
            )
            for existing_value in existing_values:
                self.db.delete(existing_value)
        if value is None:
            if capability is not None:
                self.db.delete(capability)
            self.db.flush()
            return

        capability_type = str(value["type"])
        if capability is None:
            capability = AstroEquipmentExposureCapability(
                equipment_id=equipment_id,
                tracking_mode=tracking_mode,
            )
            self.db.add(capability)
        capability.capability_type = capability_type
        capability.min_seconds = (
            self._decimal(value["min_seconds"])
            if capability_type == "range"
            else None
        )
        capability.max_seconds = (
            self._decimal(value["max_seconds"])
            if capability_type == "range"
            else None
        )
        capability.step_seconds = (
            self._decimal(value["step_seconds"])
            if capability_type == "range"
            else None
        )
        self.db.flush()
        if capability_type == "discrete":
            self.db.add_all(
                [
                    AstroEquipmentExposureValue(
                        capability_id=capability.id,
                        value_seconds=self._decimal(seconds),
                        sort_order=index,
                    )
                    for index, seconds in enumerate(value["values_seconds"])
                ]
            )
            self.db.flush()

    def delete_children(self, equipment_id: str) -> None:
        self.replace_eyepieces(equipment_id, [])
        self.replace_capability(equipment_id, "AZ", None)
        self.replace_capability(equipment_id, "EQ", None)

    @staticmethod
    def _decimal(value: object) -> Decimal:
        return Decimal(str(value))
