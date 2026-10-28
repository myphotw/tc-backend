from __future__ import annotations

from copy import deepcopy
import unittest
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.equipment import AstroEquipment
from app.astrojournal.schemas.equipment import EquipmentCreate, EquipmentUpdate
from app.astrojournal.services.equipment_service import EquipmentService
from app.common.model_registry import Base
from app.common.models.change_event import CommonChangeEvent
from app.main import app


CAPTURE_CAPABILITY = {
    "version": 1,
    "modes": [
        {
            "id": "alt_az_derotated",
            "type": "altAzDerotated",
            "label": "AZ sensor rotation correction",
            "exposure_capability": {
                "type": "free_input_range",
                "min_seconds": 0.1,
                "max_seconds": 300,
            },
        },
        {
            "id": "equatorial",
            "type": "equatorial",
            "label": "EQ",
            "exposure_capability": {
                "type": "discrete",
                "values_seconds": [1, 10, 30, 60],
            },
        },
        {
            "id": "custom_lucky",
            "type": "custom",
            "label": "Lucky imaging",
            "exposure_capability": {
                "type": "range",
                "min_seconds": 0.1,
                "max_seconds": 5,
                "step_seconds": 0.1,
            },
        },
    ],
    "gain_capability": {
        "type": "range",
        "min": 0,
        "max": 500,
        "step": 1,
    },
    "filters": [
        {"id": "off", "type": "off", "name": "Filter off"},
        {"id": "sho", "type": "narrowBand", "name": "SHO"},
    ],
    "channel_combinations": [
        {
            "id": "sho_mix",
            "name": "SHO mix",
            "primary_label": "SII",
            "secondary_label": "OIII",
            "ratio_adjustable": True,
            "min_primary_percent": 0,
            "max_primary_percent": 100,
            "step_percent": 5,
        }
    ],
}


class EquipmentCaptureCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.service = EquipmentService(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def payload(**overrides: object) -> EquipmentCreate:
        values: dict[str, object] = {
            "id": uuid4(),
            "name": "Draco",
            "kind": "smartTelescope",
            "purpose": "imaging",
            "focal_length_mm": 250,
            "fov_width_degrees": 2.24,
            "fov_height_degrees": 3.99,
            "az_exposure_capability": {
                "type": "discrete",
                "values_seconds": [1, 10, 20],
            },
            "eq_exposure_capability": {
                "type": "range",
                "min_seconds": 1,
                "max_seconds": 300,
                "step_seconds": 1,
            },
            "capture_capability": deepcopy(CAPTURE_CAPABILITY),
            **overrides,
        }
        return EquipmentCreate.model_validate(values)

    def test_create_get_and_list_round_trip_json_capability(self) -> None:
        created = self.service.create(self.payload())
        fetched = self.service.get(str(created.id))
        listed = self.service.list()

        self.assertEqual(created.capture_capability.version, 1)
        self.assertEqual(created.capture_capability.modes[0].type, "altAzDerotated")
        self.assertEqual(
            created.capture_capability.modes[0].exposure_capability.type,
            "free_input_range",
        )
        self.assertEqual(created.capture_capability.filters[1].type, "narrowBand")
        self.assertEqual(
            created.capture_capability.channel_combinations[0].step_percent,
            5,
        )
        self.assertEqual(fetched, created)
        self.assertEqual(listed, [created])
        stored = self.db.get(AstroEquipment, str(created.id))
        self.assertEqual(stored.capture_capability["version"], 1)
        self.assertEqual(
            stored.capture_capability["modes"][0]["type"],
            "altAzDerotated",
        )

    def test_patch_replaces_capability_and_emits_equipment_update(self) -> None:
        created = self.service.create(self.payload())
        replacement = deepcopy(CAPTURE_CAPABILITY)
        replacement["gain_capability"] = {
            "type": "discrete",
            "values": [0, 100, 200],
        }

        updated = self.service.update(
            str(created.id),
            EquipmentUpdate(
                expected_revision=created.revision,
                capture_capability=replacement,
            ),
        )

        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.capture_capability.gain_capability.type, "discrete")
        events = (
            self.db.query(CommonChangeEvent)
            .filter_by(resource_type="Equipment", resource_id=str(created.id))
            .order_by(CommonChangeEvent.id.asc())
            .all()
        )
        self.assertEqual(
            [(item.operation, item.revision, item.tombstone) for item in events],
            [("CREATE", 1, False), ("UPDATE", 2, False)],
        )

    def test_patch_omission_preserves_and_explicit_null_clears_capability(self) -> None:
        created = self.service.create(self.payload())

        legacy_patch = self.service.update(
            str(created.id),
            EquipmentUpdate(
                expected_revision=created.revision,
                name="Draco legacy rename",
                az_exposure_capability={
                    "type": "discrete",
                    "values_seconds": [2, 15, 30],
                },
            ),
        )
        self.assertIsNotNone(legacy_patch.capture_capability)
        self.assertEqual(legacy_patch.capture_capability.version, 1)
        self.assertEqual(legacy_patch.az_exposure_capability.values_seconds, [2, 15, 30])

        cleared = self.service.update(
            str(created.id),
            EquipmentUpdate(
                expected_revision=legacy_patch.revision,
                capture_capability=None,
            ),
        )
        self.assertEqual(cleared.revision, 3)
        self.assertIsNone(cleared.capture_capability)
        self.assertIsNone(
            self.db.get(AstroEquipment, str(created.id)).capture_capability
        )

    def test_stale_capability_patch_keeps_revision_conflict_contract(self) -> None:
        created = self.service.create(self.payload())
        self.service.update(
            str(created.id),
            EquipmentUpdate(expected_revision=1, name="Current"),
        )

        with self.assertRaises(HTTPException) as raised:
            self.service.update(
                str(created.id),
                EquipmentUpdate(
                    expected_revision=1,
                    capture_capability=deepcopy(CAPTURE_CAPABILITY),
                ),
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "REVISION_CONFLICT")
        self.assertEqual(raised.exception.detail["current_revision"], 2)

    def test_existing_az_eq_only_equipment_remains_compatible(self) -> None:
        created = self.service.create(self.payload(capture_capability=None))

        self.assertIsNone(created.capture_capability)
        self.assertEqual(created.az_exposure_capability.values_seconds, [1, 10, 20])
        self.assertEqual(created.eq_exposure_capability.type, "range")
        self.assertIn("capture_capability", created.model_dump(mode="json"))

    def test_delete_retains_existing_tombstone_contract(self) -> None:
        created = self.service.create(self.payload())
        deleted = self.service.soft_delete(str(created.id), expected_revision=1)

        self.assertTrue(deleted.deleted)
        self.assertEqual(deleted.revision, 2)
        event = (
            self.db.query(CommonChangeEvent)
            .filter_by(
                resource_type="Equipment",
                resource_id=str(created.id),
                operation="DELETE",
            )
            .one()
        )
        self.assertTrue(event.tombstone)
        self.assertEqual(event.revision, 2)

    def test_invalid_capture_capabilities_are_rejected(self) -> None:
        invalid_payloads: list[dict[str, object]] = []

        def invalid(mutator) -> None:
            value = deepcopy(CAPTURE_CAPABILITY)
            mutator(value)
            invalid_payloads.append(value)

        invalid(lambda value: value.update(version=2))
        invalid(lambda value: value["modes"][0].update(type="alt_az"))
        invalid(lambda value: value["modes"].append(deepcopy(value["modes"][0])))
        invalid(lambda value: value["modes"][0].update(id="   "))
        invalid(
            lambda value: value["modes"][0].update(
                exposure_capability={
                    "type": "discrete",
                    "values_seconds": [0],
                }
            )
        )
        invalid(
            lambda value: value["modes"][0].update(
                exposure_capability={
                    "type": "range",
                    "min_seconds": 10,
                    "max_seconds": 1,
                    "step_seconds": 1,
                }
            )
        )
        invalid(
            lambda value: value.update(
                gain_capability={"type": "range", "min": 10, "max": 1, "step": 1}
            )
        )
        invalid(
            lambda value: value.update(
                gain_capability={"type": "discrete", "values": [-1]}
            )
        )
        invalid(lambda value: value["filters"][0].update(type="lightPollution"))
        invalid(lambda value: value["filters"].append(deepcopy(value["filters"][0])))
        invalid(
            lambda value: value["channel_combinations"].append(
                deepcopy(value["channel_combinations"][0])
            )
        )
        invalid(
            lambda value: value["channel_combinations"][0].update(
                min_primary_percent=80,
                max_primary_percent=20,
            )
        )
        invalid(
            lambda value: value["channel_combinations"][0].update(step_percent=0)
        )
        invalid(
            lambda value: value["channel_combinations"][0].update(
                min_primary_percent="0"
            )
        )
        invalid(
            lambda value: value["modes"][0]["exposure_capability"].update(
                min_seconds=float("nan")
            )
        )
        invalid(
            lambda value: value["modes"][0]["exposure_capability"].update(
                max_seconds=float("inf")
            )
        )
        invalid(lambda value: value["modes"][0].update(label="x" * 70000))

        for capture_capability in invalid_payloads:
            with self.subTest(capture_capability=capture_capability):
                with self.assertRaises(ValidationError):
                    self.payload(capture_capability=capture_capability)

    def test_openapi_exposes_additive_capture_capability_contract(self) -> None:
        schemas = app.openapi()["components"]["schemas"]
        for schema_name in ("EquipmentCreate", "EquipmentUpdate", "EquipmentResponse"):
            self.assertIn("capture_capability", schemas[schema_name]["properties"])


if __name__ == "__main__":
    unittest.main()
