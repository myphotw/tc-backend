from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.vision_job import CommonVisionJob
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from scripts.backfill_capture_dates import (
    ExifScanResult,
    _apply_snapshot,
    _load_snapshot_batch,
    backfill_capture_dates,
    validate_capture_dates,
)


class LocalStorage:
    """Resolver is patched in filesystem-branch tests."""


class PresentPath:
    def is_file(self) -> bool:
        return True


class FakeExifReader:
    def __init__(self, result: dict[str, object] | None = None) -> None:
        self.result = result or {}
        self.calls: list[object] = []

    def read(self, path: object) -> dict[str, object]:
        self.calls.append(path)
        return dict(self.result)


class CaptureDateBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = LocalStorage()
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.counter = 0

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _add_file(
        self,
        *,
        service_name: str = "MemoryKeeper",
        deleted: bool = False,
        original_path: str | None = None,
        created_at: datetime | None = None,
        favorite: bool = False,
    ) -> tuple[CommonFile, CommonFileService]:
        self.counter += 1
        digest = f"{self.counter:064x}"
        common_file = CommonFile(
            file_id=digest,
            original_name=f"{self.counter}.jpg",
            service_name=service_name,
            original_path=original_path,
            created_at=created_at or datetime(2024, 1, self.counter, tzinfo=timezone.utc),
            favorite=favorite,
            deleted=deleted,
        )
        self.db.add(common_file)
        self.db.flush()
        link = CommonFileService(
            file_id=common_file.id,
            service_name=service_name,
            created_at=datetime(2024, 2, self.counter, tzinfo=timezone.utc),
        )
        self.db.add(link)
        self.db.commit()
        return common_file, link

    def test_active_memorykeeper_only_and_execute_repairs_missing_state(self) -> None:
        active, _ = self._add_file(original_path="original/active.jpg", favorite=True)
        deleted, _ = self._add_file(deleted=True, original_path="original/deleted.jpg")
        astro, _ = self._add_file(service_name="AstroJournal", original_path="original/astro.jpg")
        reader = FakeExifReader({"datetime_original": datetime(2018, 3, 4, 5, 6)})

        with patch(
            "scripts.backfill_capture_dates._resolve_original_path",
            return_value=PresentPath(),
        ):
            stats = backfill_capture_dates(
                self.db,
                storage_service=self.storage,
                exif_reader=reader,
                execute=True,
            )

        state = self.db.get(MemoryKeeperFileState, active.id)
        metadata = self.db.query(CommonFileMetadata).filter_by(file_id=active.id).one()
        self.assertEqual(stats.scanned, 1)
        self.assertEqual(stats.updated, 1)
        self.assertEqual(state.favorite, True)
        self.assertEqual(state.revision, 0)
        self.assertEqual(state.effective_capture_datetime, datetime(2018, 3, 4, 5, 6))
        self.assertEqual(state.date_basis, "EXIF")
        self.assertEqual(metadata.original_capture_datetime, datetime(2018, 3, 4, 5, 6))
        self.assertIsNone(self.db.get(MemoryKeeperFileState, deleted.id))
        self.assertIsNone(self.db.get(MemoryKeeperFileState, astro.id))
        self.assertEqual(self.db.query(CommonChangeEvent).count(), 0)
        self.assertEqual(self.db.query(CommonVisionJob).count(), 0)

    def test_user_wins_and_legacy_datetime_is_not_a_source(self) -> None:
        item, _ = self._add_file(original_path=None)
        metadata = CommonFileMetadata(
            file_id=item.id,
            datetime_original=datetime(2001, 2, 3, 4, 5),
        )
        state = MemoryKeeperFileState(
            file_id=item.id,
            favorite=False,
            revision=9,
            user_capture_datetime=datetime(2019, 4, 5, 6, 7),
            user_capture_precision="DATE",
        )
        self.db.add_all([metadata, state])
        self.db.commit()

        stats = backfill_capture_dates(
            self.db,
            storage_service=self.storage,
            execute=True,
        )

        self.assertEqual(stats.would_use_user, 1)
        self.assertEqual(state.effective_capture_datetime, datetime(2019, 4, 5, 6, 7))
        self.assertEqual(state.date_basis, "USER")
        self.assertEqual(state.user_capture_precision, "DATE")
        self.assertEqual(state.revision, 9)
        self.assertIsNone(metadata.original_capture_datetime)
        self.assertEqual(metadata.datetime_original, datetime(2001, 2, 3, 4, 5))

    def test_dry_run_is_read_only_and_reports_missing_no_exif_and_unsafe(self) -> None:
        missing, _ = self._add_file(original_path=None)
        unsafe, _ = self._add_file(original_path="outside.jpg")
        no_exif, _ = self._add_file(original_path="original/no-exif.jpg")
        reports: list[dict[str, object]] = []

        def resolve(_storage: object, stored_path: str) -> PresentPath:
            if stored_path == "outside.jpg":
                raise ValueError("outside storage")
            return PresentPath()

        with patch(
            "scripts.backfill_capture_dates._resolve_original_path",
            side_effect=resolve,
        ):
            stats = backfill_capture_dates(
                self.db,
                storage_service=self.storage,
                exif_reader=FakeExifReader({}),
                failure_reporter=reports.append,
            )

        self.assertEqual(stats.scanned, 3)
        self.assertEqual(stats.state_missing, 3)
        self.assertEqual(stats.original_file_missing, 1)
        self.assertEqual(stats.unsafe_path, 1)
        self.assertEqual(stats.no_capture_datetime, 1)
        self.assertEqual(self.db.query(MemoryKeeperFileState).count(), 0)
        self.assertEqual(self.db.query(CommonFileMetadata).count(), 0)
        self.assertEqual(
            {report["common_file_id"] for report in reports},
            {missing.id, unsafe.id, no_exif.id},
        )

    def test_projection_only_never_accesses_filesystem_and_uses_imported(self) -> None:
        item, link = self._add_file(original_path="original/unused.jpg")
        reader = FakeExifReader({"datetime_original": datetime(2010, 1, 1)})

        stats = backfill_capture_dates(
            self.db,
            storage_service=self.storage,
            exif_reader=reader,
            execute=True,
            projection_only=True,
        )

        state = self.db.get(MemoryKeeperFileState, item.id)
        self.assertEqual(reader.calls, [])
        self.assertEqual(stats.would_use_imported, 1)
        self.assertEqual(state.date_basis, "IMPORTED")
        self.assertEqual(
            state.effective_capture_datetime,
            link.created_at.replace(tzinfo=None),
        )

    def test_keyset_after_high_water_max_rows_and_idempotency(self) -> None:
        first, _ = self._add_file()
        second, _ = self._add_file()
        third, _ = self._add_file()

        first_run = backfill_capture_dates(
            self.db,
            storage_service=self.storage,
            execute=True,
            projection_only=True,
            batch_size=1,
            max_rows=2,
        )
        resumed = backfill_capture_dates(
            self.db,
            storage_service=self.storage,
            execute=True,
            projection_only=True,
            after_file_id=first_run.last_common_file_id or 0,
        )
        repeated = backfill_capture_dates(
            self.db,
            storage_service=self.storage,
            execute=True,
            projection_only=True,
        )

        self.assertEqual(first_run.scanned, 2)
        self.assertEqual(first_run.last_common_file_id, second.id)
        self.assertEqual(resumed.scanned, 1)
        self.assertEqual(resumed.last_common_file_id, third.id)
        self.assertEqual(repeated.updated, 0)
        self.assertEqual(self.db.query(MemoryKeeperFileState).count(), 3)
        self.assertEqual(first_run.high_water_common_file_id, third.id)

    def test_high_water_excludes_rows_added_during_filesystem_scan(self) -> None:
        first, _ = self._add_file(original_path="original/first.jpg")

        class AppendingReader(FakeExifReader):
            def __init__(inner_self) -> None:
                super().__init__({"datetime_original": datetime(2018, 1, 2, 3, 4)})
                inner_self.appended = False

            def read(inner_self, path: object) -> dict[str, object]:
                if not inner_self.appended:
                    inner_self.appended = True
                    self._add_file(original_path="original/new-after-high-water.jpg")
                return super().read(path)

        reader = AppendingReader()
        with patch(
            "scripts.backfill_capture_dates._resolve_original_path",
            return_value=PresentPath(),
        ):
            stats = backfill_capture_dates(
                self.db,
                storage_service=self.storage,
                exif_reader=reader,
                execute=True,
            )

        self.assertEqual(stats.high_water_common_file_id, first.id)
        self.assertEqual(stats.scanned, 1)
        self.assertEqual(self.db.query(MemoryKeeperFileState).count(), 1)

    def test_execute_failure_does_not_stop_later_rows(self) -> None:
        first, _ = self._add_file()
        second, _ = self._add_file()
        from scripts import backfill_capture_dates as module

        original_apply = module._apply_snapshot
        calls = 0

        def fail_once(*args: object, **kwargs: object):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("isolated row failure")
            return original_apply(*args, **kwargs)

        reports: list[dict[str, object]] = []
        with patch.object(module, "_apply_snapshot", side_effect=fail_once):
            stats = backfill_capture_dates(
                self.db,
                storage_service=self.storage,
                execute=True,
                projection_only=True,
                failure_reporter=reports.append,
            )

        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.updated, 1)
        self.assertIsNone(self.db.get(MemoryKeeperFileState, first.id))
        self.assertIsNotNone(self.db.get(MemoryKeeperFileState, second.id))
        self.assertEqual(reports[0]["reason"], "WRITE_ERROR:RuntimeError")

    def test_current_user_is_reloaded_before_write(self) -> None:
        item, _ = self._add_file()
        snapshot = _load_snapshot_batch(
            self.db,
            after_file_id=0,
            through_file_id=item.id,
            limit=1,
        )[0]
        self.db.rollback()
        user_state = MemoryKeeperFileState(
            file_id=item.id,
            favorite=False,
            revision=4,
            user_capture_datetime=datetime(2022, 8, 9, 10, 11),
            user_capture_precision="DATETIME",
        )
        self.db.add(user_state)
        self.db.commit()

        changed, inactive = _apply_snapshot(
            self.db,
            snapshot=snapshot,
            scan=ExifScanResult(capture_datetime=datetime(2010, 1, 2, 3, 4)),
        )
        self.db.commit()

        metadata = self.db.query(CommonFileMetadata).filter_by(file_id=item.id).one()
        self.assertTrue(changed)
        self.assertFalse(inactive)
        self.assertEqual(metadata.original_capture_datetime, datetime(2010, 1, 2, 3, 4))
        self.assertEqual(user_state.effective_capture_datetime, user_state.user_capture_datetime)
        self.assertEqual(user_state.date_basis, "USER")
        self.assertEqual(user_state.user_capture_precision, "DATETIME")
        self.assertEqual(user_state.revision, 4)

    def test_validate_only_reports_and_does_not_change_rows(self) -> None:
        item, _ = self._add_file()
        state = MemoryKeeperFileState(
            file_id=item.id,
            favorite=False,
            revision=2,
            effective_capture_datetime=datetime(2000, 1, 1),
            date_basis="EXIF",
        )
        self.db.add(state)
        self.db.commit()

        result = validate_capture_dates(self.db)

        self.assertEqual(result.active_memorykeeper_links, 1)
        self.assertEqual(result.effective_mismatch, 1)
        self.assertEqual(result.basis_mismatch, 1)
        self.assertEqual(state.revision, 2)
        self.assertEqual(state.effective_capture_datetime, datetime(2000, 1, 1))


if __name__ == "__main__":
    unittest.main()
