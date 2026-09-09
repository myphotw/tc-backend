from __future__ import annotations

import unittest
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.equipment import AstroEquipment
from app.astrojournal.models.observation_site import (
    AstroObservationSite,
    AstroObservationSiteHorizonPoint,
)
from app.astrojournal.schemas.equipment import (
    EquipmentCreate,
    EquipmentUpdate,
)
from app.astrojournal.schemas.observation_site import (
    ObservationSiteCreate,
    ObservationSiteUpdate,
)
from app.astrojournal.services.equipment_service import EquipmentService
from app.astrojournal.services.observation_site_service import ObservationSiteService
from app.common.model_registry import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.services.changes_service import ChangesService
from app.main import app


DRACO_EXPOSURES = [
    1,
    1.3,
    1.6,
    2,
    2.5,
    3.2,
    4,
    5,
    6,
    8,
    10,
    13,
    15,
    30,
    45,
    60,
    90,
    120,
    180,
    240,
    300,
]


class AstroJournalMasterDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.sites = ObservationSiteService(self.db)
        self.equipment = EquipmentService(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def equipment_payload(self, **overrides) -> EquipmentCreate:
        values = {
            "id": uuid4(),
            "name": "Draco",
            "kind": "smartTelescope",
            "purpose": "imaging",
            "focal_length_mm": 250.0,
            "aperture_mm": 50.0,
            "fov_width_degrees": 2.4,
            "fov_height_degrees": 1.8,
            "eyepieces": [],
            "az_exposure_capability": {
                "type": "discrete",
                "values_seconds": DRACO_EXPOSURES,
            },
            "eq_exposure_capability": {
                "type": "range",
                "min_seconds": 1.0,
                "max_seconds": 300.0,
                "step_seconds": 0.5,
            },
            **overrides,
        }
        return EquipmentCreate(**values)

    def site_payload(self, **overrides) -> ObservationSiteCreate:
        site_id = overrides.pop("id", uuid4())
        values = {
            "id": site_id,
            "name": "강원 관측지",
            "latitude": 37.25,
            "longitude": 128.25,
            "address": "강원도",
            "bortle": 3,
            "sqm": 21.3,
            "brightness_grade": "dark",
            "tracking_mode": "altAz",
            "default_min_altitude": 20,
            "preferred_start": "20:30",
            "preferred_end": "04:30",
            "horizon_points": [
                {
                    "id": uuid4(),
                    "observation_site_id": site_id,
                    "azimuth": 0,
                    "min_altitude": 15,
                    "max_altitude": 70,
                    "sort_order": 0,
                    "source": "camera_scan",
                },
                {
                    "id": uuid4(),
                    "observation_site_id": site_id,
                    "azimuth": 180,
                    "min_altitude": 10,
                    "sort_order": 1,
                    "source": "manual",
                },
            ],
            "blocked_azimuth_ranges": [
                {
                    "id": uuid4(),
                    "observation_site_id": site_id,
                    "start_azimuth": 350,
                    "end_azimuth": 20,
                    "reason": "북쪽 건물",
                    "source": "manual",
                }
            ],
            **overrides,
        }
        return ObservationSiteCreate(**values)

    def test_observation_site_create_get_list_and_aggregate_round_trip(self) -> None:
        created = self.sites.create(self.site_payload(default_equipment_id=None))
        fetched = self.sites.get(str(created.id))
        listed = self.sites.list()

        self.assertEqual(created.revision, 1)
        self.assertIsNone(created.default_equipment_id)
        self.assertEqual(fetched, created)
        self.assertEqual(listed, [created])
        self.assertEqual(len(created.horizon_points), 2)
        self.assertEqual(created.horizon_points[0].source, "camera_scan")
        self.assertEqual(len(created.blocked_azimuth_ranges), 1)
        self.assertEqual(created.blocked_azimuth_ranges[0].start_azimuth, 350)
        self.assertEqual(created.blocked_azimuth_ranges[0].end_azimuth, 20)

    def test_observation_site_update_replaces_children_and_checks_revision(self) -> None:
        created = self.sites.create(self.site_payload())
        replacement_id = uuid4()
        updated = self.sites.update(
            str(created.id),
            ObservationSiteUpdate(
                expected_revision=1,
                name="수정 관측지",
                default_max_altitude=80,
                horizon_points=[
                    {
                        "id": replacement_id,
                        "observation_site_id": created.id,
                        "azimuth": 90,
                        "min_altitude": 25,
                        "sort_order": 0,
                        "source": "photo_import",
                    }
                ],
                blocked_azimuth_ranges=[],
            ),
        )

        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.name, "수정 관측지")
        self.assertEqual([item.id for item in updated.horizon_points], [replacement_id])
        self.assertEqual(updated.blocked_azimuth_ranges, [])
        with self.assertRaises(HTTPException) as raised:
            self.sites.update(
                str(created.id),
                ObservationSiteUpdate(expected_revision=1, memo="stale"),
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["current_revision"], 2)

        with self.assertRaises(HTTPException) as delete_raised:
            self.sites.soft_delete(str(created.id), expected_revision=1)
        self.assertEqual(delete_raised.exception.status_code, 409)
        self.assertEqual(delete_raised.exception.detail["current_revision"], 2)

    def test_observation_site_aggregate_failure_rolls_back_replacement(self) -> None:
        first = self.sites.create(self.site_payload())
        second = self.sites.create(self.site_payload(name="두 번째"))
        original_ids = [item.id for item in first.horizon_points]

        with self.assertRaises(HTTPException) as raised:
            self.sites.update(
                str(first.id),
                ObservationSiteUpdate(
                    expected_revision=1,
                    horizon_points=[
                        {
                            "id": second.horizon_points[0].id,
                            "observation_site_id": first.id,
                            "azimuth": 45,
                            "min_altitude": 10,
                        }
                    ],
                ),
            )
        self.assertEqual(raised.exception.status_code, 409)
        reloaded = self.sites.get(str(first.id))
        self.assertEqual(reloaded.revision, 1)
        self.assertEqual([item.id for item in reloaded.horizon_points], original_ids)

    def test_observation_site_delete_is_tombstone_and_emits_changes(self) -> None:
        created = self.sites.create(self.site_payload())
        updated = self.sites.update(
            str(created.id),
            ObservationSiteUpdate(expected_revision=1, memo="updated"),
        )
        deleted = self.sites.soft_delete(
            str(created.id),
            expected_revision=updated.revision,
        )

        self.assertTrue(deleted.deleted)
        self.assertEqual(deleted.revision, 3)
        self.assertEqual(self.sites.list(), [])
        with self.assertRaises(HTTPException):
            self.sites.get(str(created.id))
        stored = self.db.get(AstroObservationSite, str(created.id))
        self.assertIsNotNone(stored.deleted_at)
        self.assertEqual(
            self.db.query(AstroObservationSiteHorizonPoint)
            .filter_by(observation_site_id=str(created.id))
            .count(),
            0,
        )
        changes = ChangesService(self.db).list_changes(service_name="AstroJournal")
        self.assertEqual(
            [(item.resource_type, item.operation, item.revision, item.tombstone) for item in changes.items],
            [
                ("ObservationSite", "CREATE", 1, False),
                ("ObservationSite", "UPDATE", 2, False),
                ("ObservationSite", "DELETE", 3, True),
            ],
        )

    def test_observation_site_validation_matches_flutter_contract(self) -> None:
        for overrides in (
            {"name": "   "},
            {"latitude": 91},
            {"longitude": -181},
            {"bortle": 10},
            {"default_min_altitude": 30, "default_max_altitude": 20},
            {"preferred_start": "25:00"},
            {
                "horizon_points": [
                    {
                        "id": uuid4(),
                        "azimuth": 360,
                        "min_altitude": 0,
                    }
                ]
            },
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    self.site_payload(**overrides)

    def test_observation_site_rejects_unknown_default_equipment_atomically(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            self.sites.create(
                self.site_payload(default_equipment_id=uuid4())
            )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(self.db.query(AstroObservationSite).count(), 0)
        self.assertEqual(self.db.query(CommonChangeEvent).count(), 0)

    def test_equipment_create_get_list_eyepiece_and_capability_round_trip(self) -> None:
        equipment_id = uuid4()
        eyepiece_id = uuid4()
        payload = self.equipment_payload(
            id=equipment_id,
            eyepieces=[
                {
                    "id": eyepiece_id,
                    "equipment_id": equipment_id,
                    "name": "20mm",
                    "focal_length_mm": 20,
                    "afov_degrees": 68,
                    "sort_order": 1,
                }
            ],
        )
        created = self.equipment.create(payload)

        self.assertEqual(self.equipment.get(str(equipment_id)), created)
        self.assertEqual(self.equipment.list(), [created])
        self.assertEqual(created.eyepieces[0].id, eyepiece_id)
        self.assertEqual(created.eyepieces[0].afov_degrees, 68)
        self.assertEqual(
            created.az_exposure_capability.values_seconds,
            [float(value) for value in DRACO_EXPOSURES],
        )
        self.assertEqual(created.eq_exposure_capability.type, "range")
        self.assertEqual(created.eq_exposure_capability.step_seconds, 0.5)
        json_payload = created.model_dump(mode="json")
        self.assertIsInstance(
            json_payload["az_exposure_capability"]["values_seconds"][1],
            float,
        )
        self.assertIsInstance(
            json_payload["eq_exposure_capability"]["step_seconds"],
            float,
        )

    def test_equipment_supports_independent_null_discrete_and_range_capabilities(self) -> None:
        created = self.equipment.create(
            self.equipment_payload(
                az_exposure_capability=None,
                eq_exposure_capability={
                    "type": "discrete",
                    "values_seconds": [20, 1.3, 10],
                },
            )
        )
        self.assertIsNone(created.az_exposure_capability)
        self.assertEqual(
            created.eq_exposure_capability.values_seconds,
            [1.3, 10.0, 20.0],
        )

        updated = self.equipment.update(
            str(created.id),
            EquipmentUpdate(
                expected_revision=1,
                az_exposure_capability={
                    "type": "range",
                    "min_seconds": 0.5,
                    "max_seconds": 30,
                    "step_seconds": 0.1,
                },
                eq_exposure_capability=None,
            ),
        )
        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.az_exposure_capability.type, "range")
        self.assertEqual(updated.az_exposure_capability.min_seconds, 0.5)
        self.assertIsNone(updated.eq_exposure_capability)

    def test_equipment_stale_update_and_delete_tombstone(self) -> None:
        created = self.equipment.create(self.equipment_payload())
        updated = self.equipment.update(
            str(created.id),
            EquipmentUpdate(expected_revision=1, name="Draco updated"),
        )
        with self.assertRaises(HTTPException) as raised:
            self.equipment.update(
                str(created.id),
                EquipmentUpdate(expected_revision=1, sort_order=2),
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["current_revision"], 2)

        with self.assertRaises(HTTPException) as delete_raised:
            self.equipment.soft_delete(str(created.id), expected_revision=1)
        self.assertEqual(delete_raised.exception.status_code, 409)
        self.assertEqual(delete_raised.exception.detail["current_revision"], 2)

        deleted = self.equipment.soft_delete(
            str(created.id),
            expected_revision=updated.revision,
        )
        self.assertEqual(deleted.revision, 3)
        self.assertEqual(self.equipment.list(), [])
        self.assertIsNotNone(self.db.get(AstroEquipment, str(created.id)).deleted_at)
        changes = (
            self.db.query(CommonChangeEvent)
            .filter_by(resource_type="Equipment")
            .order_by(CommonChangeEvent.id.asc())
            .all()
        )
        self.assertEqual(
            [(item.operation, item.revision, item.tombstone) for item in changes],
            [("CREATE", 1, False), ("UPDATE", 2, False), ("DELETE", 3, True)],
        )

    def test_equipment_aggregate_failure_rolls_back_replacement(self) -> None:
        first = self.equipment.create(
            self.equipment_payload(
                eyepieces=[
                    {
                        "id": uuid4(),
                        "name": "20mm",
                        "focal_length_mm": 20,
                        "afov_degrees": 68,
                    }
                ]
            )
        )
        second = self.equipment.create(
            self.equipment_payload(
                name="Second",
                eyepieces=[
                    {
                        "id": uuid4(),
                        "name": "10mm",
                        "focal_length_mm": 10,
                        "afov_degrees": 60,
                    }
                ],
            )
        )
        original_eyepiece_id = first.eyepieces[0].id

        with self.assertRaises(HTTPException) as raised:
            self.equipment.update(
                str(first.id),
                EquipmentUpdate(
                    expected_revision=1,
                    eyepieces=[
                        {
                            "id": second.eyepieces[0].id,
                            "equipment_id": first.id,
                            "name": "conflict",
                            "focal_length_mm": 12,
                            "afov_degrees": 55,
                        }
                    ],
                ),
            )

        self.assertEqual(raised.exception.status_code, 409)
        reloaded = self.equipment.get(str(first.id))
        self.assertEqual(reloaded.revision, 1)
        self.assertEqual(reloaded.eyepieces[0].id, original_eyepiece_id)

    def test_equipment_delete_nulls_default_site_and_emits_site_update(self) -> None:
        equipment = self.equipment.create(self.equipment_payload())
        site = self.sites.create(
            self.site_payload(default_equipment_id=equipment.id)
        )
        self.equipment.soft_delete(str(equipment.id), expected_revision=1)

        reloaded = self.sites.get(str(site.id))
        self.assertIsNone(reloaded.default_equipment_id)
        self.assertEqual(reloaded.revision, 2)
        site_events = (
            self.db.query(CommonChangeEvent)
            .filter_by(resource_type="ObservationSite", resource_id=str(site.id))
            .order_by(CommonChangeEvent.id.asc())
            .all()
        )
        self.assertEqual(
            [(item.operation, item.revision) for item in site_events],
            [("CREATE", 1), ("UPDATE", 2)],
        )

    def test_client_uuid_create_replay_is_idempotent(self) -> None:
        equipment_payload = self.equipment_payload()
        first_equipment = self.equipment.create(equipment_payload)
        replayed_equipment = self.equipment.create(
            self.equipment_payload(id=equipment_payload.id, name="ignored")
        )
        site_payload = self.site_payload()
        first_site = self.sites.create(site_payload)
        replayed_site = self.sites.create(
            self.site_payload(id=site_payload.id, name="ignored")
        )

        self.assertEqual(replayed_equipment, first_equipment)
        self.assertEqual(replayed_site, first_site)
        self.assertEqual(self.db.query(AstroEquipment).count(), 1)
        self.assertEqual(self.db.query(AstroObservationSite).count(), 1)
        self.assertEqual(
            self.db.query(CommonChangeEvent)
            .filter(CommonChangeEvent.operation == "CREATE")
            .count(),
            2,
        )

    def test_exposure_validation_rejects_invalid_values(self) -> None:
        invalid_capabilities = (
            {"type": "discrete", "values_seconds": [1.3, 1.3]},
            {"type": "discrete", "values_seconds": [0, 1]},
            {"type": "discrete", "values_seconds": [-1, 1]},
            {
                "type": "range",
                "min_seconds": 10,
                "max_seconds": 1,
                "step_seconds": 0.5,
            },
            {
                "type": "range",
                "min_seconds": 1,
                "max_seconds": 10,
                "step_seconds": 0,
            },
        )
        for capability in invalid_capabilities:
            with self.subTest(capability=capability):
                with self.assertRaises(ValidationError):
                    self.equipment_payload(az_exposure_capability=capability)

    def test_routes_are_additive_and_bearer_protected(self) -> None:
        paths = app.openapi()["paths"]
        expected = {
            "/api/astro/observation-sites": {"get", "post"},
            "/api/astro/observation-sites/{site_id}": {"get", "patch", "delete"},
            "/api/astro/equipment": {"get", "post"},
            "/api/astro/equipment/{equipment_id}": {"get", "patch", "delete"},
        }
        for path, methods in expected.items():
            self.assertEqual(set(paths[path]), methods)
            for method in methods:
                self.assertTrue(paths[path][method]["security"])
        self.assertIn("/api/astro/records", paths)
        self.assertIn("/api/astro/plate-solve", paths)


if __name__ == "__main__":
    unittest.main()
