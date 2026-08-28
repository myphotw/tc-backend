from __future__ import annotations

from datetime import datetime, timezone
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.models.plate_solve_job import AstroPlateSolveJob
from app.astrojournal.repositories.plate_solve_job_repository import PlateSolveJobStatus
from app.astrojournal.schemas.gallery import (
    AstroGalleryDetailItem,
    AstroGalleryItem,
)
from app.astrojournal.schemas.observation_record import (
    ObservationRecordDetailResponse,
    ObservationRecordResponse,
)
from app.astrojournal.services.gallery_service import AstroGalleryService
from app.astrojournal.services.observation_record_service import ObservationRecordService
from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.main import app


class PlateSolveReadProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.gallery = AstroGalleryService(self.session)
        self.records = ObservationRecordService(self.session)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _file(self, digest: str = "a" * 64) -> CommonFile:
        common_file = CommonFile(
            file_id=digest,
            original_name="astro.fits",
            mime_type="image/fits",
            deleted=False,
        )
        self.session.add(common_file)
        self.session.flush()
        self.session.add(
            CommonFileService(
                file_id=common_file.id,
                service_name="AstroJournal",
            )
        )
        return common_file

    def _record(
        self,
        common_file: CommonFile,
        *,
        status: str = "PENDING",
    ) -> ObservationRecord:
        record = ObservationRecord(
            file_id=common_file.id,
            service_name="AstroJournal",
            captured_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            plate_solve_status=status,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def _job(self, common_file: CommonFile, *, status: str) -> AstroPlateSolveJob:
        job = AstroPlateSolveJob(
            common_file_id=common_file.id,
            status=status,
            attempts=1,
        )
        if status == PlateSolveJobStatus.COMPLETED:
            job.ra = 83.822
            job.dec = -5.391
            job.rotation = 12.5
            job.pixel_scale = 1.42
            job.field_width = 2.1
            job.field_height = 1.4
            job.parity = 1.0
        self.session.add(job)
        self.session.flush()
        return job

    def test_waiting_gallery_list_and_details_expose_status_and_job(self) -> None:
        common_file = self._file()
        record = self._record(common_file, status=PlateSolveJobStatus.WAITING)
        job = self._job(common_file, status=PlateSolveJobStatus.WAITING)
        self.session.commit()

        listed = self.gallery.list_gallery().items[0]
        gallery_detail = self.gallery.get_detail(record.id)
        record_detail = self.records.get_detail(record.id)

        self.assertEqual(listed.plate_solve_status, PlateSolveJobStatus.WAITING)
        self.assertEqual(listed.plate_solve_job_id, job.id)
        self.assertNotIn("plate_solve_result", listed.model_dump())
        self.assertIsNone(gallery_detail.plate_solve_result)
        self.assertIsNone(record_detail.plate_solve_result)

    def test_processing_detail_has_no_result(self) -> None:
        common_file = self._file()
        record = self._record(common_file, status=PlateSolveJobStatus.PROCESSING)
        job = self._job(common_file, status=PlateSolveJobStatus.PROCESSING)
        self.session.commit()

        detail = self.gallery.get_detail(record.id)
        self.assertEqual(detail.plate_solve_status, PlateSolveJobStatus.PROCESSING)
        self.assertEqual(detail.plate_solve_job_id, job.id)
        self.assertIsNone(detail.plate_solve_result)

    def test_completed_details_hydrate_persisted_wcs_result(self) -> None:
        common_file = self._file()
        record = self._record(common_file, status=PlateSolveJobStatus.COMPLETED)
        job = self._job(common_file, status=PlateSolveJobStatus.COMPLETED)
        self.session.commit()

        for detail in (
            self.gallery.get_detail(record.id),
            self.records.get_detail(record.id),
        ):
            self.assertEqual(detail.plate_solve_status, PlateSolveJobStatus.COMPLETED)
            self.assertEqual(detail.plate_solve_job_id, job.id)
            self.assertEqual(detail.plate_solve_result.ra, job.ra)
            self.assertEqual(detail.plate_solve_result.dec, job.dec)
            self.assertEqual(detail.plate_solve_result.rotation, job.rotation)
            self.assertEqual(detail.plate_solve_result.pixel_scale, job.pixel_scale)
            self.assertEqual(detail.plate_solve_result.field_width, job.field_width)
            self.assertEqual(detail.plate_solve_result.field_height, job.field_height)
            self.assertEqual(detail.plate_solve_result.parity, job.parity)

    def test_failed_detail_exposes_retryable_persistent_job_id(self) -> None:
        common_file = self._file()
        record = self._record(common_file, status=PlateSolveJobStatus.FAILED)
        job = self._job(common_file, status=PlateSolveJobStatus.FAILED)
        self.session.commit()

        detail = self.gallery.get_detail(record.id)
        self.assertEqual(detail.plate_solve_status, PlateSolveJobStatus.FAILED)
        self.assertEqual(detail.plate_solve_job_id, str(job.id))
        self.assertIsInstance(detail.plate_solve_job_id, str)
        self.assertIsNone(detail.plate_solve_result)

    def test_shared_common_file_returns_same_job_and_result(self) -> None:
        common_file = self._file()
        first = self._record(common_file, status=PlateSolveJobStatus.COMPLETED)
        second = self._record(common_file, status=PlateSolveJobStatus.COMPLETED)
        job = self._job(common_file, status=PlateSolveJobStatus.COMPLETED)
        self.session.commit()

        first_detail = self.gallery.get_detail(first.id)
        second_detail = self.gallery.get_detail(second.id)
        self.assertEqual(first_detail.plate_solve_job_id, job.id)
        self.assertEqual(second_detail.plate_solve_job_id, job.id)
        self.assertEqual(
            first_detail.plate_solve_result,
            second_detail.plate_solve_result,
        )

    def test_record_without_job_keeps_status_and_returns_null_job_result(self) -> None:
        common_file = self._file()
        record = self._record(common_file, status="PENDING")
        self.session.commit()

        gallery_detail = self.gallery.get_detail(record.id)
        record_detail = self.records.get_detail(record.id)
        for detail in (gallery_detail, record_detail):
            self.assertEqual(detail.plate_solve_status, "PENDING")
            self.assertIsNone(detail.plate_solve_job_id)
            self.assertIsNone(detail.plate_solve_result)

    def test_sha_and_numeric_common_file_id_remain_distinct(self) -> None:
        digest = "f" * 64
        common_file = self._file(digest)
        record = self._record(common_file, status=PlateSolveJobStatus.WAITING)
        job = self._job(common_file, status=PlateSolveJobStatus.WAITING)
        self.session.commit()

        detail = self.gallery.get_detail(record.id)
        self.assertEqual(detail.file_id, digest)
        self.assertEqual(detail.common_file_id, common_file.id)
        self.assertEqual(job.common_file_id, common_file.id)
        self.assertNotEqual(str(detail.common_file_id), detail.file_id)

    def test_contract_extension_preserves_existing_fields_and_detail_boundary(self) -> None:
        self.assertTrue(
            set(ObservationRecordResponse.model_fields).issubset(
                ObservationRecordDetailResponse.model_fields
            )
        )
        self.assertTrue(
            {
                "file_id",
                "common_file_id",
                "plate_solve_status",
                "plate_solve_job_id",
            }.issubset(AstroGalleryItem.model_fields)
        )
        self.assertNotIn("plate_solve_result", AstroGalleryItem.model_fields)
        self.assertIn("plate_solve_result", AstroGalleryDetailItem.model_fields)
        self.assertIn(
            "plate_solve_job_id",
            ObservationRecordDetailResponse.model_fields,
        )
        self.assertIn(
            "plate_solve_result",
            ObservationRecordDetailResponse.model_fields,
        )

        schema = app.openapi()
        components = schema["components"]["schemas"]
        gallery_list_ref = schema["paths"]["/api/astro/gallery"]["get"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]["$ref"]
        gallery_detail_ref = schema["paths"]["/api/astro/gallery/{record_id}"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]["$ref"]
        record_detail_ref = schema["paths"]["/api/astro/records/{record_id}"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]["$ref"]

        self.assertTrue(gallery_list_ref.endswith("/AstroGalleryListResponse"))
        self.assertTrue(gallery_detail_ref.endswith("/AstroGalleryDetailItem"))
        self.assertTrue(record_detail_ref.endswith("/ObservationRecordDetailResponse"))
        self.assertNotIn(
            "plate_solve_result",
            components["AstroGalleryItem"]["properties"],
        )
        self.assertIn(
            "plate_solve_result",
            components["AstroGalleryDetailItem"]["properties"],
        )


if __name__ == "__main__":
    unittest.main()
