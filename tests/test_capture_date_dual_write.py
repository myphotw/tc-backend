from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.upload_job import UploadJob
from app.common.repositories.file_service_repository import FileServiceRepository
from app.common.repositories.metadata_repository import MetadataRepository
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.services.capture_date_service import (
    CaptureDateBasis,
    MemoryKeeperCaptureDateService,
    calculate_capture_date_projection,
)
from app.memorykeeper.services.file_service import MemoryKeeperFileService
from worker.plugins.base import PluginContext
from worker.plugins.exif_plugin import ExifPlugin
from worker.plugins.hash_plugin import HashPlugin
from worker.plugins.storage_plugin import StoragePlugin


class FakeStorageService:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.deleted_paths: list[str] = []

    def calculate_sha256(self, path: Path) -> str:
        return self.digest

    def delete_incoming(self, incoming_path: str) -> None:
        self.deleted_paths.append(incoming_path)

    def move_to_storage(self, **kwargs) -> Path:
        return Path("PhotoPlatform/original") / (
            f"{kwargs['file_id']}{kwargs['extension']}"
        )

    @staticmethod
    def to_relative_path(path: Path | None) -> str | None:
        return str(path).replace("\\", "/") if path is not None else None


class CaptureDateCalculationTests(unittest.TestCase):
    def test_source_priority_is_user_exif_imported_created_then_null(self) -> None:
        user = datetime(2024, 1, 2, 3, 4, 5)
        original = datetime(2023, 2, 3, 4, 5, 6)
        imported = datetime(2022, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
        created = datetime(2021, 4, 5, 6, 7, 8, tzinfo=timezone.utc)

        cases = (
            (user, original, imported, created, user, CaptureDateBasis.USER),
            (None, original, imported, created, original, CaptureDateBasis.EXIF),
            (
                None,
                None,
                imported,
                created,
                imported.replace(tzinfo=None),
                CaptureDateBasis.IMPORTED,
            ),
            (
                None,
                None,
                None,
                created,
                created.replace(tzinfo=None),
                CaptureDateBasis.CREATED,
            ),
            (None, None, None, None, None, None),
        )
        for user_value, original_value, imported_value, created_value, expected, basis in cases:
            with self.subTest(basis=basis):
                projection = calculate_capture_date_projection(
                    user_capture_datetime=user_value,
                    original_capture_datetime=original_value,
                    imported_at=imported_value,
                    created_at=created_value,
                )
                self.assertEqual(projection.effective_capture_datetime, expected)
                self.assertEqual(projection.date_basis, basis)

    def test_user_and_exif_wall_clocks_reject_aware_values(self) -> None:
        aware = datetime(2024, 1, 2, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "user_capture_datetime"):
            calculate_capture_date_projection(
                user_capture_datetime=aware,
                original_capture_datetime=None,
                imported_at=None,
                created_at=None,
            )
        with self.assertRaisesRegex(ValueError, "original_capture_datetime"):
            calculate_capture_date_projection(
                user_capture_datetime=None,
                original_capture_datetime=aware,
                imported_at=None,
                created_at=None,
            )

    def test_instant_sources_are_normalized_to_utc_naive(self) -> None:
        plus_five = timezone(timedelta(hours=5))
        imported = datetime(2024, 6, 7, 2, 30, tzinfo=plus_five)

        projection = calculate_capture_date_projection(
            user_capture_datetime=None,
            original_capture_datetime=None,
            imported_at=imported,
            created_at=None,
        )

        self.assertEqual(
            projection.effective_capture_datetime,
            datetime(2024, 6, 6, 21, 30),
        )
        self.assertEqual(projection.date_basis, CaptureDateBasis.IMPORTED)


class CaptureDateDualWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.counter = 0

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _file(
        self,
        digest: str,
        *,
        service_name: str = "MemoryKeeper",
    ) -> CommonFile:
        item = CommonFile(
            file_id=digest,
            original_name=f"{digest[:8]}.jpg",
            service_name=service_name,
            created_at=datetime(2024, 1, 2, 3, 4, tzinfo=timezone.utc),
            deleted=False,
        )
        self.db.add(item)
        self.db.flush()
        return item

    def _link(
        self,
        item: CommonFile,
        *,
        service_name: str = "MemoryKeeper",
    ) -> CommonFileService:
        link = CommonFileService(
            file_id=item.id,
            service_name=service_name,
            created_at=datetime(2024, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        self.db.add(link)
        self.db.flush()
        return link

    def _job(self, incoming_path: str, *, service_name: str = "MemoryKeeper") -> UploadJob:
        self.counter += 1
        return UploadJob(
            job_id=f"dual-write-{self.counter}",
            source_type="UPLOAD",
            status="PROCESSING",
            incoming_path=incoming_path,
            service_name=service_name,
            created_at=datetime(2024, 3, 4, 5, 6, tzinfo=timezone.utc),
        )

    def test_state_sync_preserves_user_precision_and_revision(self) -> None:
        item = self._file("1" * 64)
        link = self._link(item)
        metadata = CommonFileMetadata(
            file_id=item.id,
            original_capture_datetime=datetime(2020, 1, 2, 3, 4),
        )
        state = MemoryKeeperFileState(
            file_id=item.id,
            favorite=True,
            memo="memo",
            revision=7,
            user_capture_datetime=datetime(2019, 5, 6, 7, 8),
            user_capture_precision="DATE",
        )
        self.db.add_all([metadata, state])
        self.db.flush()

        result = MemoryKeeperCaptureDateService(self.db).synchronize(
            common_file=item,
            service_link=link,
            metadata=metadata,
            state=state,
        )

        self.assertIs(result, state)
        self.assertEqual(state.effective_capture_datetime, datetime(2019, 5, 6, 7, 8))
        self.assertEqual(state.date_basis, CaptureDateBasis.USER)
        self.assertEqual(state.user_capture_precision, "DATE")
        self.assertEqual(state.revision, 7)
        self.assertIsNone(state.effective_capture_date)
        self.assertIsNone(state.effective_capture_year)

    def test_original_capture_fact_is_null_only_and_rejects_aware(self) -> None:
        item = self._file("2" * 64)
        metadata = CommonFileMetadata(file_id=item.id)
        self.db.add(metadata)
        self.db.flush()
        repository = MetadataRepository(self.db)
        original = datetime(2022, 2, 3, 4, 5)

        repository.set_original_capture_datetime_if_missing(
            item=metadata,
            value=original,
            commit=False,
        )
        repository.set_original_capture_datetime_if_missing(
            item=metadata,
            value=datetime(2030, 1, 1),
            commit=False,
        )

        self.assertEqual(metadata.original_capture_datetime, original)
        with self.assertRaisesRegex(ValueError, "naive wall-clock"):
            empty = CommonFileMetadata(file_id=item.id)
            repository.set_original_capture_datetime_if_missing(
                item=empty,
                value=datetime(2022, 2, 3, tzinfo=timezone.utc),
                commit=False,
            )

    def test_new_memorykeeper_storage_link_creates_imported_state(self) -> None:
        digest = "3" * 64
        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory) / "new.jpg"
            incoming.write_bytes(b"new")
            context = PluginContext(
                db=self.db,
                storage_service=FakeStorageService(digest),
                incoming_path=incoming,
                job=self._job("incoming/new.jpg"),
                file_id=digest,
                original_name="new.jpg",
                extension=".jpg",
                file_size=incoming.stat().st_size,
            )

            StoragePlugin().run(context)

        state = self.db.get(MemoryKeeperFileState, context.common_file.id)
        self.assertIsNotNone(state)
        self.assertEqual(state.date_basis, CaptureDateBasis.IMPORTED)
        self.assertEqual(
            state.effective_capture_datetime,
            context.file_service_link.created_at.replace(tzinfo=None),
        )
        self.assertIs(context.memorykeeper_state, state)

        with patch(
            "worker.plugins.exif_plugin.ExifReader.read",
            return_value={},
        ):
            ExifPlugin().run(context)
        self.assertEqual(state.date_basis, CaptureDateBasis.IMPORTED)

    def test_astrojournal_storage_does_not_create_memorykeeper_state(self) -> None:
        digest = "c" * 64
        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory) / "astro.jpg"
            incoming.write_bytes(b"astro")
            context = PluginContext(
                db=self.db,
                storage_service=FakeStorageService(digest),
                incoming_path=incoming,
                job=self._job("incoming/astro.jpg", service_name="AstroJournal"),
                file_id=digest,
                original_name="astro.jpg",
                extension=".jpg",
                file_size=incoming.stat().st_size,
                service_name="AstroJournal",
            )

            StoragePlugin().run(context)

        self.assertEqual(context.file_service_link.service_name, "AstroJournal")
        self.assertIsNone(self.db.get(MemoryKeeperFileState, context.common_file.id))

    def test_restored_memorykeeper_link_uses_existing_original_fact(self) -> None:
        digest = "e" * 64
        item = self._file(digest, service_name="AstroJournal")
        item.deleted = True
        metadata = CommonFileMetadata(
            file_id=item.id,
            original_capture_datetime=datetime(2012, 6, 7, 8, 9),
        )
        self.db.add(metadata)
        self.db.commit()

        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory) / "restore.jpg"
            incoming.write_bytes(b"restore")
            context = PluginContext(
                db=self.db,
                storage_service=FakeStorageService(digest),
                incoming_path=incoming,
                job=self._job("incoming/restore.jpg"),
            )
            HashPlugin().run(context)
            self.assertTrue(context.restore_deleted_common_file)
            StoragePlugin().run(context)

        state = self.db.get(MemoryKeeperFileState, item.id)
        self.assertIsNotNone(state)
        self.assertEqual(state.date_basis, CaptureDateBasis.EXIF)
        self.assertEqual(
            state.effective_capture_datetime,
            metadata.original_capture_datetime,
        )

    def test_hash_duplicate_creates_exif_projection_for_new_link(self) -> None:
        digest = "4" * 64
        item = self._file(digest, service_name="AstroJournal")
        metadata = CommonFileMetadata(
            file_id=item.id,
            original_capture_datetime=datetime(2021, 7, 8, 9, 10),
        )
        self.db.add(metadata)
        self.db.commit()

        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory) / "duplicate.jpg"
            incoming.write_bytes(b"duplicate")
            context = PluginContext(
                db=self.db,
                storage_service=FakeStorageService(digest),
                incoming_path=incoming,
                job=self._job("incoming/duplicate.jpg"),
            )
            HashPlugin().run(context)

        state = self.db.get(MemoryKeeperFileState, item.id)
        self.assertTrue(context.stop_pipeline)
        self.assertIsNotNone(state)
        self.assertEqual(state.date_basis, CaptureDateBasis.EXIF)
        self.assertEqual(
            state.effective_capture_datetime,
            metadata.original_capture_datetime,
        )

    def test_hash_duplicate_repairs_missing_state_for_existing_link(self) -> None:
        digest = "5" * 64
        item = self._file(digest)
        self._link(item)
        self.db.commit()

        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory) / "duplicate.jpg"
            incoming.write_bytes(b"duplicate")
            context = PluginContext(
                db=self.db,
                storage_service=FakeStorageService(digest),
                incoming_path=incoming,
                job=self._job("incoming/duplicate.jpg"),
            )
            HashPlugin().run(context)

        state = self.db.get(MemoryKeeperFileState, item.id)
        self.assertIsNotNone(state)
        self.assertEqual(state.date_basis, CaptureDateBasis.IMPORTED)

    def test_storage_insert_race_reuses_file_and_repairs_projection(self) -> None:
        digest = "6" * 64
        existing = self._file(digest, service_name="AstroJournal")
        metadata = CommonFileMetadata(
            file_id=existing.id,
            original_capture_datetime=datetime(2018, 3, 4, 5, 6),
        )
        self.db.add(metadata)
        self.db.commit()

        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory) / "race.jpg"
            incoming.write_bytes(b"race")
            context = PluginContext(
                db=self.db,
                storage_service=FakeStorageService(digest),
                incoming_path=incoming,
                job=self._job("incoming/race.jpg"),
                file_id=digest,
                original_name="race.jpg",
                extension=".jpg",
                file_size=incoming.stat().st_size,
            )
            StoragePlugin().run(context)

        state = self.db.get(MemoryKeeperFileState, existing.id)
        self.assertTrue(context.stop_pipeline)
        self.assertEqual(context.common_file.id, existing.id)
        self.assertIsNotNone(state)
        self.assertEqual(state.date_basis, CaptureDateBasis.EXIF)

    def test_exif_promotes_imported_projection_and_preserves_legacy_field(self) -> None:
        item = self._file("7" * 64)
        link = self._link(item)
        state = MemoryKeeperCaptureDateService(self.db).synchronize(
            common_file=item,
            service_link=link,
            metadata=None,
            state_missing_known=True,
        )
        self.db.commit()
        selected = datetime(2017, 8, 9, 10, 11, 12)
        context = PluginContext(
            db=self.db,
            storage_service=FakeStorageService(item.file_id),
            common_file=item,
            original_path=Path("original.jpg"),
            service_name="MemoryKeeper",
            file_service_link=link,
            memorykeeper_state=state,
        )

        with patch(
            "worker.plugins.exif_plugin.ExifReader.read",
            return_value={"datetime_original": selected},
        ):
            ExifPlugin().run(context)

        metadata = self.db.query(CommonFileMetadata).filter_by(file_id=item.id).one()
        self.assertEqual(metadata.datetime_original, selected)
        self.assertEqual(metadata.original_capture_datetime, selected)
        self.assertEqual(state.effective_capture_datetime, selected)
        self.assertEqual(state.date_basis, CaptureDateBasis.EXIF)

    def test_exif_original_fact_is_recorded_when_legacy_metadata_is_locked(self) -> None:
        item = self._file("f" * 64)
        link = self._link(item)
        legacy = datetime(2011, 1, 2, 3, 4)
        metadata = CommonFileMetadata(
            file_id=item.id,
            datetime_original=legacy,
            original_capture_datetime=None,
            locked=True,
        )
        self.db.add(metadata)
        state = MemoryKeeperCaptureDateService(self.db).synchronize(
            common_file=item,
            service_link=link,
            metadata=metadata,
            state_missing_known=True,
        )
        self.db.commit()
        selected = datetime(2019, 9, 10, 11, 12)
        context = PluginContext(
            db=self.db,
            storage_service=FakeStorageService(item.file_id),
            common_file=item,
            original_path=Path("locked-original.jpg"),
            service_name="MemoryKeeper",
            file_service_link=link,
            memorykeeper_state=state,
        )

        with patch(
            "worker.plugins.exif_plugin.ExifReader.read",
            return_value={"datetime_original": selected},
        ):
            ExifPlugin().run(context)

        self.assertEqual(metadata.datetime_original, legacy)
        self.assertEqual(metadata.original_capture_datetime, selected)
        self.assertEqual(state.effective_capture_datetime, selected)
        self.assertEqual(state.date_basis, CaptureDateBasis.EXIF)

    def test_lazy_state_creation_uses_existing_original_fact(self) -> None:
        item = self._file("8" * 64)
        self._link(item)
        metadata = CommonFileMetadata(
            file_id=item.id,
            original_capture_datetime=datetime(2016, 4, 5, 6, 7),
        )
        self.db.add(metadata)
        self.db.commit()

        state = MemoryKeeperFileService(self.db).get_state(item, create=True)

        self.assertIsNotNone(state)
        self.assertEqual(state.effective_capture_datetime, metadata.original_capture_datetime)
        self.assertEqual(state.date_basis, CaptureDateBasis.EXIF)

    def test_lazy_state_does_not_treat_legacy_datetime_as_original_fact(self) -> None:
        item = self._file("d" * 64)
        link = self._link(item)
        metadata = CommonFileMetadata(
            file_id=item.id,
            datetime_original=datetime(2010, 1, 2, 3, 4),
            original_capture_datetime=None,
        )
        self.db.add(metadata)
        self.db.commit()

        state = MemoryKeeperFileService(self.db).get_state(item, create=True)

        self.assertIsNotNone(state)
        self.assertEqual(state.date_basis, CaptureDateBasis.IMPORTED)
        self.assertEqual(
            state.effective_capture_datetime,
            link.created_at.replace(tzinfo=None),
        )

    def test_astrojournal_exif_writes_common_fact_without_memorykeeper_state(self) -> None:
        item = self._file("9" * 64, service_name="AstroJournal")
        link = self._link(item, service_name="AstroJournal")
        self.db.commit()
        selected = datetime(2015, 2, 3, 4, 5)
        context = PluginContext(
            db=self.db,
            storage_service=FakeStorageService(item.file_id),
            common_file=item,
            original_path=Path("astro.jpg"),
            service_name="AstroJournal",
            file_service_link=link,
        )

        with patch(
            "worker.plugins.exif_plugin.ExifReader.read",
            return_value={"datetime_original": selected},
        ):
            ExifPlugin().run(context)

        metadata = self.db.query(CommonFileMetadata).filter_by(file_id=item.id).one()
        self.assertEqual(metadata.datetime_original, selected)
        self.assertEqual(metadata.original_capture_datetime, selected)
        self.assertIsNone(self.db.get(MemoryKeeperFileState, item.id))

    def test_link_and_initial_state_roll_back_together_on_projection_failure(self) -> None:
        digest = "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory) / "rollback.jpg"
            incoming.write_bytes(b"rollback")
            context = PluginContext(
                db=self.db,
                storage_service=FakeStorageService(digest),
                incoming_path=incoming,
                job=self._job("incoming/rollback.jpg"),
                file_id=digest,
                original_name="rollback.jpg",
                extension=".jpg",
                file_size=incoming.stat().st_size,
            )
            with patch(
                "worker.plugins.storage_plugin.MemoryKeeperCaptureDateService.synchronize",
                side_effect=RuntimeError("projection failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "projection failed"):
                    StoragePlugin().run(context)

        item = self.db.query(CommonFile).filter_by(file_id=digest).one()
        self.assertIsNone(
            FileServiceRepository(self.db).get(
                file_id=item.id,
                service_name="MemoryKeeper",
            )
        )
        self.assertIsNone(self.db.get(MemoryKeeperFileState, item.id))

    def test_exif_fact_and_projection_roll_back_together_on_failure(self) -> None:
        item = self._file("b" * 64)
        link = self._link(item)
        state = MemoryKeeperCaptureDateService(self.db).synchronize(
            common_file=item,
            service_link=link,
            metadata=None,
            state_missing_known=True,
        )
        self.db.commit()
        context = PluginContext(
            db=self.db,
            storage_service=FakeStorageService(item.file_id),
            common_file=item,
            original_path=Path("rollback-exif.jpg"),
            service_name="MemoryKeeper",
            file_service_link=link,
            memorykeeper_state=state,
        )

        with patch(
            "worker.plugins.exif_plugin.ExifReader.read",
            return_value={"datetime_original": datetime(2014, 1, 2, 3, 4)},
        ), patch(
            "worker.plugins.exif_plugin.MemoryKeeperCaptureDateService.synchronize",
            side_effect=RuntimeError("projection failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "projection failed"):
                ExifPlugin().run(context)

        self.db.expire_all()
        metadata = self.db.query(CommonFileMetadata).filter_by(file_id=item.id).one_or_none()
        self.assertTrue(
            metadata is None
            or (
                metadata.datetime_original is None
                and metadata.original_capture_datetime is None
            )
        )


if __name__ == "__main__":
    unittest.main()
