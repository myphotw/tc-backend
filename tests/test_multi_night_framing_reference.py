from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.multi_night_framing_reference import (
    AstroMultiNightFramingReference,
)
from app.astrojournal.schemas.equipment import EquipmentCreate
from app.astrojournal.schemas.multi_night_framing_reference import (
    MultiNightFramingReferenceCreate,
    MultiNightFramingReferenceUpdate,
)
from app.astrojournal.schemas.observation_site import ObservationSiteCreate
from app.astrojournal.services.equipment_service import EquipmentService
from app.astrojournal.services.multi_night_framing_reference_service import (
    MultiNightFramingReferenceService,
)
from app.astrojournal.services.observation_site_service import ObservationSiteService
from app.common.model_registry import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.services.changes_service import ChangesService
from app.main import app


REFERENCE_TIME = datetime(2026, 7, 15, 21, 10, tzinfo=timezone(timedelta(hours=9)))


class MultiNightFramingReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.sites = ObservationSiteService(self.db)
        self.equipment = EquipmentService(self.db)
        self.references = MultiNightFramingReferenceService(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def create_site(self, *, name: str = "강원 관측지"):
        return self.sites.create(
            ObservationSiteCreate(
                id=uuid4(),
                name=name,
                latitude=37.25,
                longitude=128.25,
            )
        )

    def create_equipment(self, *, name: str = "Draco", is_active: bool = True):
        return self.equipment.create(
            EquipmentCreate(
                id=uuid4(),
                name=name,
                kind="smartTelescope",
                purpose="imaging",
                is_active=is_active,
            )
        )

    def reference_payload(
        self,
        *,
        site_id=None,
        equipment_id=None,
        **overrides,
    ) -> MultiNightFramingReferenceCreate:
        values = {
            "id": uuid4(),
            "catalog_object_id": "M16",
            "reference_captured_at": REFERENCE_TIME,
            "site_id": site_id or self.create_site().id,
            "equipment_id": equipment_id or self.create_equipment().id,
            "reference_hour_angle_deg": -9.25,
            "reference_parallactic_angle_deg": 132.4,
            "reference_branch": "rising",
            **overrides,
        }
        return MultiNightFramingReferenceCreate(**values)

    def reference_events(self, reference_id: str) -> list[CommonChangeEvent]:
        return (
            self.db.query(CommonChangeEvent)
            .filter_by(
                resource_type="MultiNightFramingReference",
                resource_id=reference_id,
            )
            .order_by(CommonChangeEvent.id.asc())
            .all()
        )

    def test_create_get_list_and_target_equipment_filters(self) -> None:
        site = self.create_site()
        draco = self.create_equipment()
        seestar = self.create_equipment(name="Seestar")
        first = self.references.create(
            self.reference_payload(site_id=site.id, equipment_id=draco.id)
        )
        second = self.references.create(
            self.reference_payload(
                site_id=site.id,
                equipment_id=seestar.id,
                catalog_object_id="M31",
                reference_captured_at=REFERENCE_TIME - timedelta(days=1),
            )
        )

        self.assertEqual(self.references.get(str(first.id)), first)
        self.assertEqual(self.references.list(), [first, second])
        self.assertEqual(
            self.references.list(catalog_object_id=" M16 "),
            [first],
        )
        self.assertEqual(
            self.references.list(
                catalog_object_id="M16",
                equipment_id=draco.id,
            ),
            [first],
        )
        self.assertEqual(
            [(event.operation, event.revision) for event in self.reference_events(str(first.id))],
            [("CREATE", 1)],
        )

    def test_client_uuid_replay_and_logical_duplicate_are_distinct(self) -> None:
        site = self.create_site()
        equipment = self.create_equipment()
        payload = self.reference_payload(
            site_id=site.id,
            equipment_id=equipment.id,
        )
        first = self.references.create(payload)
        replay = self.references.create(
            self.reference_payload(
                id=payload.id,
                site_id=site.id,
                equipment_id=equipment.id,
                reference_hour_angle_deg=10,
            )
        )

        self.assertEqual(replay, first)
        self.assertEqual(self.db.query(AstroMultiNightFramingReference).count(), 1)

        with self.assertRaises(HTTPException) as raised:
            self.references.create(
                self.reference_payload(
                    site_id=self.create_site(name="다른 관측지").id,
                    equipment_id=equipment.id,
                )
            )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "REFERENCE_ALREADY_EXISTS")
        self.assertEqual(raised.exception.detail["reference_id"], str(first.id))

    def test_soft_deleted_identity_can_be_registered_with_a_new_uuid(self) -> None:
        site = self.create_site()
        equipment = self.create_equipment()
        first = self.references.create(
            self.reference_payload(site_id=site.id, equipment_id=equipment.id)
        )
        self.references.soft_delete(str(first.id), expected_revision=1)

        replacement = self.references.create(
            self.reference_payload(site_id=site.id, equipment_id=equipment.id)
        )

        self.assertNotEqual(replacement.id, first.id)
        self.assertEqual(self.references.list(catalog_object_id="M16"), [replacement])

    def test_update_increments_revision_and_rejects_stale_mutations(self) -> None:
        reference = self.references.create(self.reference_payload())
        replacement_site = self.create_site(name="새 관측지")
        updated = self.references.update(
            str(reference.id),
            MultiNightFramingReferenceUpdate(
                expected_revision=1,
                site_id=replacement_site.id,
                reference_captured_at=REFERENCE_TIME + timedelta(hours=1),
                reference_hour_angle_deg=5.5,
                reference_parallactic_angle_deg=-20.25,
                reference_branch="setting",
            ),
        )

        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.site_id, replacement_site.id)
        self.assertEqual(updated.reference_hour_angle_deg, 5.5)
        self.assertEqual(updated.reference_branch, "setting")

        with self.assertRaises(HTTPException) as patch_error:
            self.references.update(
                str(reference.id),
                MultiNightFramingReferenceUpdate(
                    expected_revision=1,
                    reference_branch="rising",
                ),
            )
        self.assertEqual(patch_error.exception.status_code, 409)
        self.assertEqual(patch_error.exception.detail["current_revision"], 2)

        with self.assertRaises(HTTPException) as delete_error:
            self.references.soft_delete(str(reference.id), expected_revision=1)
        self.assertEqual(delete_error.exception.status_code, 409)
        self.assertEqual(delete_error.exception.detail["current_revision"], 2)

    def test_equipment_change_rechecks_logical_uniqueness(self) -> None:
        site = self.create_site()
        draco = self.create_equipment()
        seestar = self.create_equipment(name="Seestar")
        draco_reference = self.references.create(
            self.reference_payload(site_id=site.id, equipment_id=draco.id)
        )
        seestar_reference = self.references.create(
            self.reference_payload(site_id=site.id, equipment_id=seestar.id)
        )

        with self.assertRaises(HTTPException) as raised:
            self.references.update(
                str(seestar_reference.id),
                MultiNightFramingReferenceUpdate(
                    expected_revision=1,
                    equipment_id=draco.id,
                ),
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "REFERENCE_ALREADY_EXISTS")
        self.assertEqual(
            raised.exception.detail["reference_id"],
            str(draco_reference.id),
        )
        self.assertEqual(self.references.get(str(seestar_reference.id)).revision, 1)

    def test_delete_is_tombstone_and_emits_complete_change_sequence(self) -> None:
        reference = self.references.create(self.reference_payload())
        updated = self.references.update(
            str(reference.id),
            MultiNightFramingReferenceUpdate(
                expected_revision=1,
                reference_branch="setting",
            ),
        )
        deleted = self.references.soft_delete(
            str(reference.id),
            expected_revision=updated.revision,
        )

        self.assertTrue(deleted.deleted)
        self.assertEqual(deleted.revision, 3)
        self.assertEqual(self.references.list(), [])
        with self.assertRaises(HTTPException):
            self.references.get(str(reference.id))
        stored = self.db.get(AstroMultiNightFramingReference, str(reference.id))
        self.assertIsNotNone(stored.deleted_at)
        self.assertEqual(
            [
                (event.operation, event.revision, event.tombstone)
                for event in self.reference_events(str(reference.id))
            ],
            [
                ("CREATE", 1, False),
                ("UPDATE", 2, False),
                ("DELETE", 3, True),
            ],
        )
        self.assertTrue(
            all(
                event.service_name == "AstroJournal"
                for event in self.reference_events(str(reference.id))
            )
        )
        feed_items = [
            item
            for item in ChangesService(self.db)
            .list_changes(service_name="AstroJournal", limit=500)
            .items
            if item.resource_type == "MultiNightFramingReference"
            and item.resource_id == str(reference.id)
        ]
        self.assertEqual(
            [(item.operation, item.revision, item.tombstone) for item in feed_items],
            [
                ("CREATE", 1, False),
                ("UPDATE", 2, False),
                ("DELETE", 3, True),
            ],
        )

    def test_reference_validates_target_angles_branch_and_capture_timezone(self) -> None:
        site = self.create_site()
        equipment = self.create_equipment()
        invalid_values = (
            {"catalog_object_id": "   "},
            {"reference_hour_angle_deg": -181},
            {"reference_hour_angle_deg": 181},
            {"reference_parallactic_angle_deg": -181},
            {"reference_parallactic_angle_deg": 181},
            {"reference_branch": "transit"},
            {"reference_captured_at": datetime(2026, 7, 15, 21, 10)},
            {
                "reference_captured_at": datetime.now(timezone.utc)
                + timedelta(hours=1)
            },
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValidationError):
                    self.reference_payload(
                        site_id=site.id,
                        equipment_id=equipment.id,
                        **overrides,
                    )

        with self.assertRaises(ValidationError):
            MultiNightFramingReferenceUpdate.model_validate(
                {
                    "expected_revision": 1,
                    "catalog_object_id": "M31",
                }
            )

    def test_create_rejects_missing_or_deleted_site_and_equipment(self) -> None:
        active_site = self.create_site()
        active_equipment = self.create_equipment()
        inactive_equipment = self.create_equipment(
            name="비활성 장비",
            is_active=False,
        )
        deleted_site = self.create_site(name="삭제 관측지")
        deleted_equipment = self.create_equipment(name="삭제 장비")
        self.sites.soft_delete(str(deleted_site.id), expected_revision=1)
        self.equipment.soft_delete(str(deleted_equipment.id), expected_revision=1)

        cases = (
            {"site_id": uuid4(), "equipment_id": active_equipment.id},
            {"site_id": deleted_site.id, "equipment_id": active_equipment.id},
            {"site_id": active_site.id, "equipment_id": uuid4()},
            {"site_id": active_site.id, "equipment_id": inactive_equipment.id},
            {"site_id": active_site.id, "equipment_id": deleted_equipment.id},
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(HTTPException) as raised:
                    self.references.create(self.reference_payload(**values))
                self.assertEqual(raised.exception.status_code, 422)

    def test_update_rejects_deleted_site_or_inactive_equipment(self) -> None:
        reference = self.references.create(self.reference_payload())
        deleted_site = self.create_site(name="삭제 관측지")
        deleted_equipment = self.create_equipment(name="삭제 장비")
        inactive_equipment = self.create_equipment(
            name="비활성 장비",
            is_active=False,
        )
        self.sites.soft_delete(str(deleted_site.id), expected_revision=1)
        self.equipment.soft_delete(str(deleted_equipment.id), expected_revision=1)

        for field_name, resource_id in (
            ("site_id", deleted_site.id),
            ("equipment_id", deleted_equipment.id),
            ("equipment_id", inactive_equipment.id),
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaises(HTTPException) as raised:
                    self.references.update(
                        str(reference.id),
                        MultiNightFramingReferenceUpdate(
                            expected_revision=1,
                            **{field_name: resource_id},
                        ),
                    )
                self.assertEqual(raised.exception.status_code, 422)
                self.assertEqual(self.references.get(str(reference.id)).revision, 1)

    def test_master_soft_delete_preserves_existing_reference(self) -> None:
        site = self.create_site()
        equipment = self.create_equipment()
        reference = self.references.create(
            self.reference_payload(site_id=site.id, equipment_id=equipment.id)
        )

        self.sites.soft_delete(str(site.id), expected_revision=1)
        self.equipment.soft_delete(str(equipment.id), expected_revision=1)

        preserved = self.references.get(str(reference.id))
        self.assertEqual(preserved.site_id, site.id)
        self.assertEqual(preserved.equipment_id, equipment.id)
        updated = self.references.update(
            str(reference.id),
            MultiNightFramingReferenceUpdate(
                expected_revision=1,
                reference_parallactic_angle_deg=120,
            ),
        )
        self.assertEqual(updated.revision, 2)

    def test_tombstoned_client_uuid_cannot_be_reused(self) -> None:
        payload = self.reference_payload()
        reference = self.references.create(payload)
        self.references.soft_delete(str(reference.id), expected_revision=1)

        with self.assertRaises(HTTPException) as raised:
            self.references.create(payload)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "RESOURCE_TOMBSTONED")

    def test_routes_are_additive_and_bearer_protected(self) -> None:
        paths = app.openapi()["paths"]
        collection_path = "/api/astro/multi-night-framing-references"
        detail_path = (
            "/api/astro/multi-night-framing-references/{reference_id}"
        )

        self.assertEqual(set(paths[collection_path]), {"get", "post"})
        self.assertEqual(set(paths[detail_path]), {"get", "patch", "delete"})
        for path in (collection_path, detail_path):
            for operation in paths[path].values():
                self.assertTrue(operation["security"])
        self.assertIn("/api/astro/observation-sites", paths)
        self.assertIn("/api/astro/equipment", paths)
        self.assertIn("/api/astro/plate-solve", paths)


if __name__ == "__main__":
    unittest.main()
