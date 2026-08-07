from __future__ import annotations

from datetime import datetime, timezone
import unittest
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.routers.observation_records import delete_record
from app.astrojournal.schemas.observation_record import (
    ObservationRecordCreate,
    ObservationRecordResponse,
    ObservationRecordUpdate,
)
from app.astrojournal.services.gallery_service import AstroGalleryService
from app.astrojournal.services.observation_record_service import ObservationRecordService
from app.common.database import Base
from app.common.models.file import CommonFile
from app.main import app


class SprintB501Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.service = ObservationRecordService(self.session)
        self.file = CommonFile(file_id="c" * 64, original_name="mutation.fits")
        self.session.add(self.file)
        self.session.commit()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _create(self, **overrides) -> ObservationRecord:
        values = {
            "file_id": self.file.id,
            "catalog_object_id": "M42",
            "captured_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
            **overrides,
        }
        return self.service.create(ObservationRecordCreate(**values))

    def test_partial_patch_updates_fields_revision_and_updated_at(self) -> None:
        record = self._create(memo="before")
        previous_updated_at = record.updated_at
        captured_at = datetime(2026, 4, 2, tzinfo=timezone.utc)

        updated = self.service.update(
            record.id,
            ObservationRecordUpdate(
                revision=1,
                catalog_object_id="M31",
                captured_at=captured_at,
                latitude=35.1,
                longitude=128.2,
                location_name="New site",
                memo="after",
                favorite=True,
            ),
        )

        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.catalog_object_id, "M31")
        self.assertEqual(updated.captured_at.replace(tzinfo=timezone.utc), captured_at)
        self.assertEqual((updated.latitude, updated.longitude), (35.1, 128.2))
        self.assertEqual(updated.location_name, "New site")
        self.assertEqual(updated.memo, "after")
        self.assertTrue(updated.favorite)
        self.assertGreaterEqual(
            updated.updated_at.replace(tzinfo=None),
            previous_updated_at.replace(tzinfo=None),
        )

    def test_stale_revision_returns_current_revision_detail(self) -> None:
        record = self._create()
        self.service.update(
            record.id,
            ObservationRecordUpdate(revision=1, memo="current"),
        )

        with self.assertRaises(HTTPException) as raised:
            self.service.update(
                record.id,
                ObservationRecordUpdate(revision=1, memo="stale"),
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail,
            {
                "code": "REVISION_CONFLICT",
                "record_id": record.id,
                "expected_revision": 1,
                "current_revision": 2,
            },
        )

    def test_representative_patch_keeps_one_active_record_per_catalog(self) -> None:
        first = self._create(representative=True)
        second = self._create()

        updated = self.service.update(
            second.id,
            ObservationRecordUpdate(revision=1, representative=True),
        )
        self.session.expire_all()

        self.assertTrue(updated.representative)
        self.assertFalse(self.session.get(ObservationRecord, first.id).representative)
        representatives = (
            self.session.query(ObservationRecord)
            .filter(ObservationRecord.catalog_object_id == "M42")
            .filter(ObservationRecord.representative.is_(True))
            .filter(ObservationRecord.deleted_at.is_(None))
            .count()
        )
        self.assertEqual(representatives, 1)

    def test_delete_is_soft_idempotent_and_excluded_from_reads(self) -> None:
        record = self._create()

        first = delete_record(record.id, db=self.session)
        second = delete_record(record.id, db=self.session)

        self.assertTrue(first.deleted)
        self.assertEqual(first.record_id, UUID(record.id))
        self.assertEqual(first.revision, 2)
        self.assertEqual(second.revision, first.revision)
        self.assertEqual(second.deleted_at, first.deleted_at)
        self.assertEqual(self.service.list(), [])
        with self.assertRaises(HTTPException) as raised:
            AstroGalleryService(self.session).get_detail(record.id)
        self.assertEqual(raised.exception.status_code, 404)

    def test_client_record_id_replay_does_not_duplicate_record(self) -> None:
        client_record_id = uuid4()
        payload = ObservationRecordCreate(
            file_id=self.file.id,
            client_record_id=client_record_id,
            catalog_object_id="M42",
            captured_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            memo="original",
        )

        first = self.service.create(payload)
        replay = self.service.create(payload.model_copy(update={"memo": "ignored"}))
        another = self.service.create(
            payload.model_copy(update={"client_record_id": uuid4()})
        )

        self.assertEqual(replay.id, first.id)
        self.assertEqual(replay.memo, "original")
        self.assertNotEqual(another.id, first.id)
        self.assertEqual(self.session.query(ObservationRecord).count(), 2)

    def test_response_openapi_and_indexes_expose_final_contract(self) -> None:
        record = self._create(client_record_id=uuid4())
        response = ObservationRecordResponse.model_validate(record)
        self.assertEqual(response.record_id, response.id)
        self.assertIsNotNone(response.client_record_id)

        paths = app.openapi()["paths"]
        self.assertIn("post", paths["/api/astro/records"])
        self.assertIn("patch", paths["/api/astro/records/{record_id}"])
        self.assertIn(
            "409",
            paths["/api/astro/records/{record_id}"]["patch"]["responses"],
        )
        self.assertEqual(
            set(paths["/api/astro/records/{record_id}"]["delete"]["responses"]),
            {"200", "422"},
        )
        update_properties = app.openapi()["components"]["schemas"][
            "ObservationRecordUpdate"
        ]["properties"]
        create_properties = app.openapi()["components"]["schemas"][
            "ObservationRecordCreate"
        ]["properties"]
        response_properties = app.openapi()["components"]["schemas"][
            "ObservationRecordResponse"
        ]["properties"]
        self.assertIn("revision", update_properties)
        self.assertIn("plate_solve_status", update_properties)
        self.assertIn("client_record_id", create_properties)
        self.assertIn("record_id", response_properties)

        index_names = {
            item["name"]
            for item in inspect(self.engine).get_indexes("astro_observation_records")
        }
        self.assertIn(
            "uq_astro_observation_records_active_representative",
            index_names,
        )
        self.assertIn(
            "uq_astro_observation_records_service_client_record_id",
            index_names,
        )
