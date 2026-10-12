from __future__ import annotations

from datetime import datetime, timezone
import unittest
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.schemas.equipment import EquipmentCreate, EquipmentUpdate
from app.astrojournal.schemas.observation_record import (
    ObservationRecordCreate,
    ObservationRecordUpdate,
)
from app.astrojournal.services.equipment_service import EquipmentService
from app.astrojournal.services.gallery_service import AstroGalleryService
from app.astrojournal.services.observation_record_service import (
    ObservationRecordService,
)
from app.common.model_registry import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.schema_sync import (
    bootstrap_metadata_projection,
    is_migration_managed,
)
from app.main import app


class AstroGalleryGeneralInfoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.records = ObservationRecordService(self.db)
        self.equipment = EquipmentService(self.db)
        self.gallery = AstroGalleryService(self.db)
        self.file = CommonFile(
            file_id="e" * 64,
            original_name="general-info.fits",
            mime_type="image/fits",
        )
        self.db.add(self.file)
        self.db.flush()
        self.db.add(
            CommonFileService(
                file_id=self.file.id,
                service_name="AstroJournal",
            )
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _equipment(self, **overrides):
        values = {
            "id": uuid4(),
            "name": "Draco",
            "kind": "smartTelescope",
            "purpose": "imaging",
            "is_active": True,
            **overrides,
        }
        return self.equipment.create(EquipmentCreate(**values))

    def _record(self, **overrides) -> ObservationRecord:
        values = {
            "file_id": self.file.id,
            "catalog_object_id": "M42",
            "captured_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
            **overrides,
        }
        return self.records.create(ObservationRecordCreate(**values))

    def test_create_links_canonical_equipment_and_uses_server_snapshot(self) -> None:
        equipment = self._equipment(name="Canonical Draco")

        record = self._record(
            equipment_ref_id=equipment.id,
            equipment_name_snapshot="Injected name",
            integration_seconds=0,
            address="Seoul Observatory",
        )

        self.assertEqual(str(record.equipment_ref_id), str(equipment.id))
        self.assertEqual(record.equipment_name_snapshot, "Canonical Draco")
        self.assertEqual(record.integration_seconds, 0)
        self.assertEqual(record.address, "Seoul Observatory")
        item = self.gallery.get_detail(record.id)
        self.assertEqual(item.equipment_name, "Canonical Draco")
        self.assertEqual(item.integration_seconds, 0)
        self.assertEqual(item.address, "Seoul Observatory")
        detail = self.records.get_detail(record.id)
        self.assertEqual(str(detail.equipment_ref_id), str(equipment.id))
        self.assertEqual(detail.equipment_name_snapshot, "Canonical Draco")
        self.assertEqual(detail.integration_seconds, 0)
        self.assertEqual(detail.address, "Seoul Observatory")

    def test_patch_links_equipment_and_preserves_omitted_general_info(self) -> None:
        equipment = self._equipment()
        record = self._record(
            integration_seconds=3600,
            address="Original address",
        )

        updated = self.records.update(
            record.id,
            ObservationRecordUpdate(
                revision=record.revision,
                equipment_ref_id=equipment.id,
                memo="linked",
            ),
        )

        self.assertEqual(updated.equipment_ref_id, str(equipment.id))
        self.assertEqual(updated.equipment_name_snapshot, equipment.name)
        self.assertEqual(updated.integration_seconds, 3600)
        self.assertEqual(updated.address, "Original address")

    def test_missing_inactive_and_deleted_equipment_cannot_be_selected(self) -> None:
        inactive = self._equipment(name="Inactive", is_active=False)
        deleted = self._equipment(name="Deleted")
        self.equipment.soft_delete(str(deleted.id), expected_revision=1)

        for equipment_id in (uuid4(), inactive.id, deleted.id):
            with self.subTest(equipment_id=equipment_id):
                with self.assertRaises(HTTPException) as raised:
                    self._record(
                        client_record_id=uuid4(),
                        equipment_ref_id=equipment_id,
                    )
                self.assertEqual(raised.exception.status_code, 422)
                self.assertEqual(
                    raised.exception.detail["code"],
                    "ACTIVE_EQUIPMENT_NOT_FOUND",
                )

    def test_equipment_rename_changes_gallery_without_record_mutation(self) -> None:
        equipment = self._equipment(name="Before rename")
        record = self._record(equipment_ref_id=equipment.id)
        before_event_count = (
            self.db.query(CommonChangeEvent)
            .filter_by(resource_type="ObservationRecord", resource_id=record.id)
            .count()
        )

        renamed = self.equipment.update(
            str(equipment.id),
            EquipmentUpdate(expected_revision=1, name="After rename"),
        )
        self.db.expire_all()
        item = self.gallery.get_detail(record.id)
        reloaded = self.db.get(ObservationRecord, record.id)

        self.assertEqual(item.equipment_name, "After rename")
        self.assertEqual(item.equipment_name_snapshot, "Before rename")
        self.assertEqual(reloaded.revision, 1)
        self.assertEqual(
            self.db.query(CommonChangeEvent)
            .filter_by(resource_type="ObservationRecord", resource_id=record.id)
            .count(),
            before_event_count,
        )
        equipment_events = (
            self.db.query(CommonChangeEvent)
            .filter_by(resource_type="Equipment", resource_id=str(equipment.id))
            .order_by(CommonChangeEvent.id.asc())
            .all()
        )
        self.assertEqual(
            [(event.operation, event.revision) for event in equipment_events],
            [("CREATE", 1), ("UPDATE", renamed.revision)],
        )

    def test_soft_deleted_equipment_name_remains_in_gallery(self) -> None:
        equipment = self._equipment(name="Preserved name")
        record = self._record(equipment_ref_id=equipment.id)

        self.equipment.soft_delete(str(equipment.id), expected_revision=1)
        item = self.gallery.get_detail(record.id)

        self.assertEqual(str(item.equipment_ref_id), str(equipment.id))
        self.assertEqual(item.equipment_name, "Preserved name")
        self.assertFalse(item.equipment_is_active)
        self.assertTrue(item.equipment_deleted)

    def test_inactive_link_is_preserved_but_snapshot_cannot_be_injected(self) -> None:
        equipment = self._equipment(name="Linked equipment")
        record = self._record(equipment_ref_id=equipment.id)
        self.equipment.update(
            str(equipment.id),
            EquipmentUpdate(expected_revision=1, is_active=False),
        )

        updated = self.records.update(
            record.id,
            ObservationRecordUpdate(revision=record.revision, memo="still linked"),
        )
        item = self.gallery.get_detail(record.id)
        self.assertEqual(updated.equipment_ref_id, str(equipment.id))
        self.assertEqual(item.equipment_name, "Linked equipment")
        self.assertFalse(item.equipment_is_active)

        with self.assertRaises(HTTPException) as raised:
            self.records.update(
                record.id,
                ObservationRecordUpdate(
                    revision=updated.revision,
                    equipment_name_snapshot="Injected name",
                ),
            )
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(
            raised.exception.detail["code"],
            "EQUIPMENT_SNAPSHOT_SERVER_MANAGED",
        )

    def test_legacy_equipment_value_is_not_auto_linked_by_name(self) -> None:
        self._equipment(name="Same legacy name")
        record = self._record(equipment_id="Same legacy name")

        item = self.gallery.get_detail(record.id)

        self.assertIsNone(record.equipment_ref_id)
        self.assertIsNone(item.equipment_ref_id)
        self.assertEqual(item.equipment_name, "Same legacy name")
        self.assertIsNone(item.equipment_is_active)
        self.assertIsNone(item.equipment_deleted)

    def test_unlink_preserves_latest_authoritative_equipment_name(self) -> None:
        equipment = self._equipment(name="Initial name")
        record = self._record(
            equipment_ref_id=equipment.id,
            equipment_id="Legacy value",
        )
        self.equipment.update(
            str(equipment.id),
            EquipmentUpdate(expected_revision=1, name="Latest name"),
        )

        updated = self.records.update(
            record.id,
            ObservationRecordUpdate(
                revision=record.revision,
                equipment_ref_id=None,
                equipment_name_snapshot="Injected unlink name",
            ),
        )
        item = self.gallery.get_detail(record.id)

        self.assertIsNone(updated.equipment_ref_id)
        self.assertEqual(updated.equipment_name_snapshot, "Latest name")
        self.assertEqual(updated.equipment_id, "Legacy value")
        self.assertEqual(item.equipment_name, "Latest name")

    def test_nullable_general_info_distinguishes_omitted_from_explicit_null(self) -> None:
        record = self._record(
            integration_seconds=1200,
            address="Address",
        )
        omitted = self.records.update(
            record.id,
            ObservationRecordUpdate(revision=1, memo="only memo"),
        )
        self.assertEqual(omitted.integration_seconds, 1200)
        self.assertEqual(omitted.address, "Address")

        cleared = self.records.update(
            record.id,
            ObservationRecordUpdate(
                revision=2,
                integration_seconds=None,
                address=None,
            ),
        )
        self.assertIsNone(cleared.integration_seconds)
        self.assertIsNone(cleared.address)

    def test_integration_seconds_rejects_negative_and_fractional_values(self) -> None:
        for value in (-1, 1.5, "10", True):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    ObservationRecordCreate(
                        file_id=self.file.id,
                        captured_at=datetime.now(timezone.utc),
                        integration_seconds=value,
                    )

    def test_existing_payload_and_target_capture_patch_remain_compatible(self) -> None:
        record = self._record(memo="legacy payload")
        captured_at = datetime(2026, 10, 2, tzinfo=timezone.utc)

        updated = self.records.update(
            record.id,
            ObservationRecordUpdate(
                revision=1,
                captured_at=captured_at,
                catalog_object_id="M31",
            ),
        )

        self.assertEqual(updated.catalog_object_id, "M31")
        self.assertEqual(updated.captured_at.replace(tzinfo=timezone.utc), captured_at)
        self.assertIsNone(updated.equipment_ref_id)
        self.assertIsNone(updated.integration_seconds)
        self.assertIsNone(updated.address)

    def test_new_columns_and_index_are_migration_owned(self) -> None:
        table = ObservationRecord.__table__
        for column_name in (
            "equipment_ref_id",
            "equipment_name_snapshot",
            "integration_seconds",
            "address",
        ):
            self.assertTrue(is_migration_managed(table.c[column_name]))
        index = next(
            item
            for item in table.indexes
            if item.name == "ix_astro_observation_records_equipment_ref_id"
        )
        self.assertTrue(is_migration_managed(index))

        bootstrap_table = bootstrap_metadata_projection(Base.metadata).tables[
            "astro_observation_records"
        ]
        for column_name in (
            "equipment_ref_id",
            "equipment_name_snapshot",
            "integration_seconds",
            "address",
        ):
            self.assertTrue(bootstrap_table.c[column_name].system)

    def test_openapi_contract_adds_general_info_without_removing_legacy_fields(self) -> None:
        schemas = app.openapi()["components"]["schemas"]
        expected = {
            "equipment_ref_id",
            "equipment_name_snapshot",
            "integration_seconds",
            "address",
        }
        for schema_name in (
            "ObservationRecordCreate",
            "ObservationRecordUpdate",
            "ObservationRecordResponse",
        ):
            properties = set(schemas[schema_name]["properties"])
            self.assertTrue(expected <= properties)
            self.assertIn("equipment_id", properties)

        gallery_properties = set(schemas["AstroGalleryItem"]["properties"])
        gallery_expected = expected | {
            "equipment_name",
            "equipment_is_active",
            "equipment_deleted",
        }
        self.assertTrue(gallery_expected <= gallery_properties)


if __name__ == "__main__":
    unittest.main()
