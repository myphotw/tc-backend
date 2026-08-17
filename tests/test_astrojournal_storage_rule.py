from __future__ import annotations

import io
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import UploadFile
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.observation_record import ObservationRecord
from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.upload_job import UploadJob
from app.common.routers import upload as upload_router
from app.common.services.storage.storage_rule import AstroJournalStorageRule
from app.common.services.storage.storage_rule_engine import StorageRuleEngine
from app.common.services.storage_service import StorageService
from app.common.services.upload_metadata import decode_upload_metadata
from worker import background_worker


class LocalStorageService(StorageService):
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def storage_root(self) -> Path:
        return self.root

    @property
    def incoming_root(self) -> Path:
        return self.root / "incoming"

    @property
    def original_root(self) -> Path:
        return self.root / "original"

    @property
    def preview_root(self) -> Path:
        return self.root / "preview"

    @property
    def thumb_root(self) -> Path:
        return self.root / "thumb"


class AstroJournalStorageRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = AstroJournalStorageRule()
        self.created_at = datetime(2025, 8, 17, tzinfo=timezone.utc)

    def context(self, metadata: dict | None = None, **kwargs):
        values = {
            "metadata": metadata or {},
            "job": SimpleNamespace(created_at=self.created_at),
            "service_name": "AstroJournal",
            "processing_log": [],
        }
        values.update(kwargs)
        return SimpleNamespace(**values)

    def test_normal_metadata_uses_namespace_year_and_canonical_target(self) -> None:
        context = self.context(
            {
                "observation_date": "2024-11-03",
                "canonical_target_id": "M42",
            }
        )

        self.assertEqual(
            self.rule.build_path(context),
            "AstroJournal/2024/M42",
        )

    def test_target_segments_are_sanitized(self) -> None:
        context = self.context(
            {
                "canonical_target_id": '안드로메다/M31*?<>|"',
            }
        )

        path = self.rule.build_path(context)

        self.assertEqual(
            path,
            "AstroJournal/2025/안드로메다_M31______",
        )
        self.assertFalse(
            any(token in path for token in ('\\', ':', '*', '?', '"', '<', '>', '|'))
        )

    def test_target_segment_handles_reserved_and_long_values(self) -> None:
        reserved = self.rule.build_path(
            self.context({"canonical_target_id": "CON.txt"})
        )
        long_target = self.rule.build_path(
            self.context({"canonical_target_id": "한" * 200})
        ).split("/")[-1]

        self.assertEqual(reserved, "AstroJournal/2025/_CON.txt")
        self.assertLessEqual(len(long_target.encode("utf-8")), 180)

    def test_missing_target_metadata_uses_unknown(self) -> None:
        self.assertEqual(
            self.rule.build_path(self.context()),
            "AstroJournal/2025/Unknown",
        )

    def test_display_name_is_used_when_canonical_target_is_missing(self) -> None:
        context = self.context({"target_display_name": "오리온 성운"})

        self.assertEqual(
            self.rule.build_path(context),
            "AstroJournal/2025/오리온 성운",
        )

    def test_observation_date_has_priority_over_exif_datetime(self) -> None:
        context = self.context(
            {
                "observation_date": "2026-08-17",
                "datetime_original": "2021-01-02T03:04:05",
            }
        )

        self.assertEqual(self.rule.build_path(context).split("/")[1], "2026")

    def test_datetime_original_controls_year(self) -> None:
        context = self.context({"datetime_original": datetime(2021, 1, 2)})

        self.assertEqual(self.rule.build_path(context).split("/")[1], "2021")

    def test_missing_datetime_uses_upload_job_created_at(self) -> None:
        self.assertEqual(self.rule.build_path(self.context()).split("/")[1], "2025")

    def test_memorykeeper_context_keeps_existing_rule(self) -> None:
        context = self.context(
            {
                "datetime_original": "2023-01-01",
                "country": "Korea",
                "city": "Seoul",
                "place_name": "Home",
            },
            service_name="MemoryKeeper",
        )

        self.assertEqual(
            StorageRuleEngine().build_path(context),
            "2023/Korea/Seoul/Home",
        )

    def test_unknown_service_fails_closed(self) -> None:
        context = self.context(
            {
                "datetime_original": "2022-01-01",
                "country": "Korea",
                "city": "Busan",
                "place_name": "Harbor",
            },
            service_name="UnknownService",
        )

        with self.assertRaisesRegex(ValueError, "Unsupported storage service"):
            StorageRuleEngine().build_path(context)

        self.assertIn("RULE_UNSUPPORTED UnknownService", context.processing_log)
        self.assertNotIn("RULE_SELECTED MemoryKeeper", context.processing_log)

    def test_same_filename_uses_hash_targets_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalStorageService(Path(directory))
            first = storage.incoming_root / "same.jpg"
            first.parent.mkdir(parents=True)
            first.write_bytes(b"first")
            second = storage.incoming_root / "same-again.jpg"
            second.write_bytes(b"second")

            first_target = storage.move_to_storage(
                first,
                "a" * 64,
                ".jpg",
                relative_dir="AstroJournal/2025/Unknown",
            )
            second_target = storage.move_to_storage(
                second,
                "b" * 64,
                ".jpg",
                relative_dir="AstroJournal/2025/Unknown",
            )

            self.assertNotEqual(first_target, second_target)
            self.assertEqual(first_target.read_bytes(), b"first")
            self.assertEqual(second_target.read_bytes(), b"second")

    def test_incoming_filename_sanitizer_handles_reserved_and_long_names(self) -> None:
        storage = LocalStorageService(Path("unused"))

        self.assertEqual(storage._sanitize_filename("CON.jpg"), "_CON.jpg")
        sanitized = storage._sanitize_filename(f"{'한' * 200}:photo.jpg")
        self.assertLessEqual(len(sanitized.encode("utf-8")), 180)
        self.assertTrue(sanitized.endswith(".jpg"))
        self.assertNotIn(":", sanitized)


class AstroJournalPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session = self.session_factory()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_astro_upload_pipeline_moves_media_and_completes_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalStorageService(Path(directory))
            image_bytes = io.BytesIO()
            Image.new("RGB", (32, 24), color="navy").save(image_bytes, format="JPEG")
            image_bytes.seek(0)

            with patch.object(upload_router, "storage_service", storage):
                response = upload_router.upload_file(
                    file=UploadFile(filename="astro.jpg", file=image_bytes),
                    service_name="AstroJournal",
                    client_file_id="astro-m31-20260817",
                    client_content_sha256=None,
                    observation_date=date(2026, 8, 17),
                    canonical_target_id="M31",
                    target_display_name="Andromeda Galaxy",
                    db=self.session,
                )
            job = self.session.query(UploadJob).filter_by(job_id=response["job_id"]).one()

            self.assertEqual(
                decode_upload_metadata(job.processing_log),
                {
                    "observation_date": "2026-08-17",
                    "canonical_target_id": "M31",
                    "target_display_name": "Andromeda Galaxy",
                },
            )

            with patch.object(
                background_worker,
                "StorageService",
                return_value=storage,
            ), patch.object(
                background_worker,
                "SessionLocal",
                self.session_factory,
            ):
                claimed = background_worker.process_next_job(
                    worker_id="astro-test-worker",
                    db=self.session,
                )

            self.session.refresh(job)
            common_file = self.session.query(CommonFile).one()
            link = self.session.query(CommonFileService).one()

            self.assertTrue(claimed)
            self.assertEqual(job.status, "COMPLETED")
            self.assertIn("CLAIMED worker=astro-test-worker", job.processing_log)
            self.assertEqual(job.file_id, common_file.file_id)
            self.assertIn("PLUGIN_COMPLETE StoragePlugin", job.processing_log)
            self.assertTrue(
                common_file.original_path.startswith(
                    "original/AstroJournal/2026/M31/"
                )
            )
            self.assertTrue(common_file.preview_path.startswith("preview/"))
            self.assertTrue(common_file.thumb_path.startswith("thumb/"))
            self.assertTrue(storage.resolve_storage_path(common_file.original_path).exists())
            self.assertTrue(storage.resolve_storage_path(common_file.preview_path).exists())
            self.assertTrue(storage.resolve_storage_path(common_file.thumb_path).exists())
            self.assertEqual(link.service_name, "AstroJournal")
            self.assertEqual(self.session.query(ObservationRecord).count(), 0)

    def test_unknown_service_job_is_failed_without_memorykeeper_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = LocalStorageService(Path(directory))
            incoming = storage.incoming_root / "typo.jpg"
            incoming.parent.mkdir(parents=True)
            Image.new("RGB", (16, 16), color="black").save(incoming)
            job = UploadJob(
                job_id="a1000000-0000-0000-0000-000000000002",
                source_type="UPLOAD",
                status="WAITING",
                incoming_path="incoming/typo.jpg",
                service_name="AstroJornal",
            )
            self.session.add(job)
            self.session.commit()

            with patch.object(
                background_worker,
                "StorageService",
                return_value=storage,
            ), patch.object(
                background_worker,
                "SessionLocal",
                self.session_factory,
            ):
                claimed = background_worker.process_next_job(
                    worker_id="astro-test-worker",
                    db=self.session,
                )

            self.session.refresh(job)
            self.assertTrue(claimed)
            self.assertEqual(job.status, "FAILED")
            self.assertIn("Unsupported storage service: AstroJornal", job.error_message)
            self.assertIn("RULE_UNSUPPORTED AstroJornal", job.processing_log)
            self.assertEqual(self.session.query(CommonFile).count(), 0)
            self.assertTrue(incoming.exists())


if __name__ == "__main__":
    unittest.main()
