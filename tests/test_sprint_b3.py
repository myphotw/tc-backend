from __future__ import annotations

from datetime import datetime, timezone
import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.schemas.observation_record import (
    ObservationRecordCreate,
    ObservationRecordUpdate,
)
from app.astrojournal.services.observation_record_service import ObservationRecordService
from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.schema_sync import initialize_database
from app.main import app


class SprintB3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.service = ObservationRecordService(self.session)
        self.file = CommonFile(file_id="b" * 64, original_name="astro.jpg")
        self.session.add(self.file)
        self.session.commit()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _create(self, **overrides) -> ObservationRecord:
        values = {
            "catalog_object_id": "M42",
            "captured_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            **overrides,
        }
        payload = ObservationRecordCreate(
            file_id=self.file.id,
            **values,
        )
        return self.service.create(payload)

    def test_create_adds_astro_domain_link(self) -> None:
        record = self._create(memo="first light")

        self.assertEqual(record.service_name, "AstroJournal")
        self.assertEqual(record.revision, 1)
        link = (
            self.session.query(CommonFileService)
            .filter(CommonFileService.file_id == self.file.id)
            .filter(CommonFileService.service_name == "AstroJournal")
            .one()
        )
        self.assertEqual(link.file_id, self.file.id)

    def test_update_uses_revision_and_conflict_returns_409(self) -> None:
        record = self._create()
        updated = self.service.update(
            record.id,
            ObservationRecordUpdate(revision=record.revision, memo="updated"),
        )

        self.assertEqual(updated.memo, "updated")
        self.assertEqual(updated.revision, 2)
        with self.assertRaises(HTTPException) as raised:
            self.service.update(
                record.id,
                ObservationRecordUpdate(revision=1, memo="stale"),
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_representative_is_unique_per_catalog_object(self) -> None:
        first = self._create(representative=True)
        second = self._create(representative=True)
        self.session.expire_all()

        self.assertFalse(self.session.get(ObservationRecord, first.id).representative)
        self.assertTrue(self.session.get(ObservationRecord, second.id).representative)

    def test_list_excludes_soft_deleted_records(self) -> None:
        active = self._create(favorite=True)
        deleted = self._create(catalog_object_id="M31")
        deleted_result = self.service.soft_delete(deleted.id)

        self.assertIsNotNone(deleted_result.deleted_at)
        self.assertEqual(self.service.list(favorite=True), [active])
        self.assertEqual(len(self.service.list()), 1)
        with self.assertRaises(HTTPException) as raised:
            self.service.get(deleted.id)
        self.assertEqual(raised.exception.status_code, 404)

    def test_openapi_registers_only_astro_record_endpoints(self) -> None:
        paths = app.openapi()["paths"]
        self.assertEqual(
            set(paths["/api/astro/records"]),
            {"get", "post"},
        )
        self.assertEqual(
            set(paths["/api/astro/records/{record_id}"]),
            {"get", "patch", "delete"},
        )

    def test_schema_contains_observation_indexes(self) -> None:
        initialize_database(self.engine)
        inspector = inspect(self.engine)
        columns = {
            column["name"]
            for column in inspector.get_columns("astro_observation_records")
        }
        indexes = {
            index["name"]
            for index in inspector.get_indexes("astro_observation_records")
        }

        self.assertTrue(
            {
                "file_id",
                "catalog_object_id",
                "captured_at",
                "favorite",
                "representative",
                "revision",
                "deleted_at",
            }.issubset(columns)
        )
        self.assertTrue(
            {
                "ix_astro_observation_records_created_at",
                "ix_astro_observation_records_catalog_object_id",
                "ix_astro_observation_records_captured_at",
                "ix_astro_observation_records_favorite",
                "ix_astro_observation_records_catalog_representative",
            }.issubset(indexes)
        )
