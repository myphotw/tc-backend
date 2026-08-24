from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.common.config import settings
from app.common.database import Base, get_db
from app.common.models.api_usage import CommonApiUsage
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.models.vision_job import CommonVisionJob
from app.common.models.worker_status import CommonWorkerStatus
from app.common.repositories.api_usage_repository import ApiName, ApiProvider
from app.common.repositories.tag_repository import TagRepository, TagSource, TagType
from app.common.repositories.vision_job_repository import VisionJobStatus
from app.common.services.gallery_service import GalleryService
from app.main import app
from app.memorykeeper.services.auto_tag_service import MemoryKeeperAutoTagService
from worker.vision_worker import process_next_vision_job


class _AsgiResponse:
    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self):
        return json.loads(self.content)


def _request_app(method: str, url: str) -> _AsgiResponse:
    parsed = urlsplit(url)
    messages: list[dict[str, object]] = []
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": parsed.path,
        "raw_path": parsed.path.encode("ascii"),
        "query_string": parsed.query.encode("ascii"),
        "root_path": "",
        "headers": [],
        "client": ("test-client", 50000),
        "server": ("test-server", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = next(item for item in messages if item["type"] == "http.response.start")
    content = b"".join(
        item.get("body", b"")
        for item in messages
        if item["type"] == "http.response.body"
    )
    return _AsgiResponse(int(start["status"]), content)


class _FakeVisionPlugins:
    def run(self, context) -> None:
        TagRepository(context.db).save_ai_tag(
            file_id=context.common_file.id,
            tag="Dog",
            confidence=96,
        )
        context.log("VISION_COMPLETE")


class MemoryKeeperAutoTagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.service = MemoryKeeperAutoTagService(self.db)
        self.counter = 0
        self.original_token = settings.TC_BACKEND_AUTH_TOKEN

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        settings.TC_BACKEND_AUTH_TOKEN = self.original_token
        self.db.close()
        self.engine.dispose()

    def file(self, services: tuple[str, ...] = ("MemoryKeeper",)) -> CommonFile:
        self.counter += 1
        digest = f"{self.counter:064x}"
        item = CommonFile(
            file_id=digest,
            original_name=f"{digest}.jpg",
            service_name=services[0],
            deleted=False,
        )
        self.db.add(item)
        self.db.flush()
        for service in services:
            self.db.add(CommonFileService(file_id=item.id, service_name=service))
        self.db.commit()
        return item

    def job(
        self,
        file: CommonFile,
        status: str,
        *,
        retry_count: int = 0,
        error: str | None = None,
        completed_at: datetime | None = None,
    ) -> CommonVisionJob:
        item = CommonVisionJob(
            file_id=file.id,
            priority=10,
            status=status,
            retry_count=retry_count,
            vision_provider="GOOGLE",
            requested_at=datetime.now(timezone.utc),
            completed_at=completed_at,
            last_error=error,
            deleted=False,
        )
        self.db.add(item)
        self.db.commit()
        return item

    def raw(self, file: CommonFile, name: str, confidence: float = 95) -> CommonFileTag:
        item = CommonFileTag(
            file_id=file.id,
            tag=name,
            tag_type=TagType.AI,
            source=TagSource.AI,
            confidence=confidence,
            deleted=False,
        )
        self.db.add(item)
        self.db.commit()
        return item

    def test_status_uses_memorykeeper_scope_usage_credential_and_worker(self) -> None:
        memory = self.file()
        astro = self.file(("AstroJournal",))
        now = datetime.now(timezone.utc)
        self.job(memory, VisionJobStatus.WAITING)
        self.job(memory, VisionJobStatus.PROCESSING)
        self.job(memory, VisionJobStatus.FAILED, retry_count=1, completed_at=now)
        self.job(memory, VisionJobStatus.COMPLETED, completed_at=now)
        self.job(astro, VisionJobStatus.FAILED, retry_count=1, completed_at=now)
        self.db.add(
            CommonApiUsage(
                provider=ApiProvider.GOOGLE,
                api_name=ApiName.VISION,
                year=now.year,
                month=now.month,
                used_unit=56,
                limit_unit=999,
                remaining_unit=943,
                deleted=False,
            )
        )
        self.db.add(
            CommonWorkerStatus(
                worker_name="VisionWorker",
                status="RUNNING",
                last_heartbeat=now,
                processed_count=1,
                failed_count=1,
            )
        )
        self.db.commit()

        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "vision.json"
            credential.write_text("{}", encoding="utf-8")
            with (
                patch.object(settings, "GOOGLE_VISION_CREDENTIAL", str(credential)),
                patch.object(settings, "VISION_MONTHLY_LIMIT", 1000),
            ):
                result = self.service.status()

        self.assertTrue(result.service_available)
        self.assertTrue(result.credential_ready)
        self.assertTrue(result.worker_online)
        self.assertEqual(result.waiting_count, 1)
        self.assertEqual(result.processing_count, 1)
        self.assertEqual(result.failed_count, 1)
        self.assertEqual(result.today_completed_count, 1)
        self.assertEqual((result.monthly_usage, result.monthly_limit), (56, 900))
        self.assertEqual(result.monthly_remaining, 844)
        self.assertTrue(result.quota_available)
        self.assertFalse(result.monthly_limit_reached)
        self.assertEqual(result.quota_waiting_count, 0)
        self.assertEqual(result.curation_version, 1)
        self.assertIsNotNone(result.last_processed_at)
        self.assertIsNotNone(result.last_failure_at)
        self.assertEqual(self.db.query(CommonApiUsage).count(), 1)

    def test_missing_credential_and_offline_worker_make_service_unavailable(self) -> None:
        self.db.add(
            CommonWorkerStatus(
                worker_name="VisionWorker",
                status="RUNNING",
                last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=61),
                processed_count=0,
                failed_count=0,
            )
        )
        self.db.commit()

        with patch.object(settings, "GOOGLE_VISION_CREDENTIAL", None):
            result = self.service.status()

        self.assertFalse(result.credential_ready)
        self.assertFalse(result.worker_online)
        self.assertFalse(result.service_available)
        self.assertEqual(result.monthly_usage, 0)
        self.assertEqual(self.db.query(CommonApiUsage).count(), 0)

    def test_quota_exhaustion_is_not_service_failure_and_marks_waiting(self) -> None:
        memory = self.file()
        self.job(memory, VisionJobStatus.WAITING)
        now = datetime.now(timezone.utc)
        self.db.add(
            CommonApiUsage(
                provider=ApiProvider.GOOGLE,
                api_name=ApiName.VISION,
                year=now.year,
                month=now.month,
                used_unit=900,
                limit_unit=900,
                remaining_unit=0,
                deleted=False,
            )
        )
        self.db.add(
            CommonWorkerStatus(
                worker_name="VisionWorker",
                status="RUNNING",
                last_heartbeat=now,
                processed_count=0,
                failed_count=0,
            )
        )
        self.db.commit()

        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "vision.json"
            credential.write_text("{}", encoding="utf-8")
            with patch.object(settings, "GOOGLE_VISION_CREDENTIAL", str(credential)):
                result = self.service.status()

        self.assertTrue(result.service_available)
        self.assertTrue(result.credential_ready)
        self.assertTrue(result.worker_online)
        self.assertFalse(result.quota_available)
        self.assertTrue(result.monthly_limit_reached)
        self.assertEqual(result.quota_waiting_count, 1)
        self.assertEqual(result.waiting_count, 1)
        self.assertEqual(result.failed_count, 0)

    def test_failed_list_is_paginated_service_scoped_and_redacted(self) -> None:
        first = self.file()
        second = self.file()
        astro = self.file(("AstroJournal",))
        secret_error = (
            "traceback private_key=SECRET postgresql://user:password@private-host/db"
        )
        self.job(first, VisionJobStatus.FAILED, retry_count=1, error=secret_error)
        self.job(second, VisionJobStatus.FAILED, retry_count=3, error="timeout")
        self.job(astro, VisionJobStatus.FAILED, retry_count=1, error="credential SECRET")

        page = self.service.failed_jobs(page=1, page_size=1)

        self.assertEqual(page.total, 2)
        self.assertEqual(len(page.items), 1)
        serialized = page.model_dump_json()
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("private-host", serialized)
        self.assertNotIn("password", serialized)
        self.assertIn(page.items[0].safe_error_code, {"PROCESSING_ERROR", "NETWORK_ERROR"})
        listed = self.service.failed_jobs(page=1, page_size=10)
        self.assertEqual({item.file_id for item in listed.items}, {first.file_id, second.file_id})
        exhausted = next(item for item in listed.items if item.retry_count == 3)
        self.assertFalse(exhausted.retryable)

    def test_bulk_retry_respects_limit_status_service_and_preserves_raw(self) -> None:
        retryable_file = self.file()
        exhausted_file = self.file()
        astro = self.file(("AstroJournal",))
        raw = self.raw(retryable_file, "Dog", 91)
        retryable = self.job(
            retryable_file,
            VisionJobStatus.FAILED,
            retry_count=1,
            error="temporary provider error",
        )
        exhausted = self.job(
            exhausted_file,
            VisionJobStatus.FAILED,
            retry_count=3,
            error="timeout",
        )
        astro_failed = self.job(
            astro,
            VisionJobStatus.FAILED,
            retry_count=1,
            error="timeout",
        )
        waiting = self.job(retryable_file, VisionJobStatus.WAITING, retry_count=0)

        result = self.service.retry_failed(limit=100)

        self.assertEqual(result.requested_count, 2)
        self.assertEqual(result.requeued_count, 1)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.failed_count, 1)
        self.db.refresh(retryable)
        self.db.refresh(exhausted)
        self.db.refresh(astro_failed)
        self.db.refresh(waiting)
        self.db.refresh(raw)
        self.assertEqual(retryable.status, VisionJobStatus.WAITING)
        self.assertEqual(retryable.retry_count, 1)
        self.assertIsNone(retryable.last_error)
        self.assertEqual(exhausted.status, VisionJobStatus.FAILED)
        self.assertEqual(astro_failed.status, VisionJobStatus.FAILED)
        self.assertEqual(waiting.status, VisionJobStatus.WAITING)
        self.assertEqual(waiting.retry_count, 0)
        self.assertFalse(raw.deleted)
        self.assertEqual((raw.tag, raw.confidence), ("Dog", 91))

    def test_individual_retry_skips_nonfailed_and_rejects_astro(self) -> None:
        memory = self.file()
        astro = self.file(("AstroJournal",))
        completed = self.job(memory, VisionJobStatus.COMPLETED)
        processing = self.job(memory, VisionJobStatus.PROCESSING)
        astro_failed = self.job(astro, VisionJobStatus.FAILED, retry_count=1)

        completed_result = self.service.retry_job(completed.id)
        processing_result = self.service.retry_job(processing.id)

        self.assertEqual(completed_result.skipped_count, 1)
        self.assertEqual(processing_result.skipped_count, 1)
        with self.assertRaisesRegex(Exception, "MemoryKeeper Vision job not found"):
            self.service.retry_job(astro_failed.id)

    def test_retry_success_updates_projection_and_emits_memorykeeper_change(self) -> None:
        memory = self.file()
        failed = self.job(
            memory,
            VisionJobStatus.FAILED,
            retry_count=1,
            error="temporary provider error",
        )
        self.service.retry_job(failed.id)

        with (
            patch(
                "worker.vision_worker.ApiUsageRepository.can_use",
                return_value=True,
            ),
            patch(
                "worker.vision_worker.PluginManager.load_plugins",
                return_value=_FakeVisionPlugins(),
            ),
        ):
            processed = process_next_vision_job(self.db)

        self.assertTrue(processed)
        self.db.refresh(failed)
        self.assertEqual(failed.status, VisionJobStatus.COMPLETED)
        detail = GalleryService(self.db).get_detail(
            memory.file_id,
            service_name="MemoryKeeper",
        )
        self.assertEqual([tag.tag for tag in detail.tags], ["강아지"])
        event = self.db.query(CommonChangeEvent).one()
        self.assertEqual(event.service_name, "MemoryKeeper")
        self.assertEqual(event.resource_type, "MemoryKeeperFileTag")
        self.assertEqual(event.operation, "UPDATE")
        self.assertFalse(event.tombstone)

    def test_curation_preview_is_bounded_read_only_and_excludes_astro(self) -> None:
        mapped = self.file()
        unmapped = self.file()
        astro = self.file(("AstroJournal",))
        self.raw(mapped, "Dog")
        self.raw(unmapped, "Unknown technical label")
        self.raw(astro, "Cat")
        now = datetime.now(timezone.utc)
        self.db.add(
            CommonApiUsage(
                provider=ApiProvider.GOOGLE,
                api_name=ApiName.VISION,
                year=now.year,
                month=now.month,
                used_unit=900,
                limit_unit=900,
                remaining_unit=0,
                deleted=False,
            )
        )
        self.db.commit()
        before = [
            (row.id, row.tag, row.confidence, row.deleted)
            for row in self.db.query(CommonFileTag).order_by(CommonFileTag.id).all()
        ]

        limited = self.service.curation_preview(sample_limit=1)
        full = self.service.curation_preview(sample_limit=10)

        self.assertEqual(limited.files_with_raw_tags, 2)
        self.assertEqual(limited.current_raw_tag_count, 2)
        self.assertEqual(limited.evaluated_file_count, 1)
        self.assertTrue(limited.has_more)
        self.assertEqual(full.evaluated_file_count, 2)
        self.assertFalse(full.has_more)
        self.assertEqual(full.projected_curated_tag_count, 1)
        self.assertEqual(full.zero_tag_file_count, 1)
        self.assertEqual(full.mapped_percentage, 50.0)
        after = [
            (row.id, row.tag, row.confidence, row.deleted)
            for row in self.db.query(CommonFileTag).order_by(CommonFileTag.id).all()
        ]
        self.assertEqual(after, before)

    def test_routes_openapi_auth_and_status_response(self) -> None:
        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        settings.TC_BACKEND_AUTH_TOKEN = None
        with patch.object(settings, "GOOGLE_VISION_CREDENTIAL", None):
            response = _request_app("GET", "/api/memorykeeper/auto-tags/status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["credential_ready"])

        settings.TC_BACKEND_AUTH_TOKEN = "auto-tag-test-token"
        unauthorized = _request_app("GET", "/api/memorykeeper/auto-tags/failed")
        self.assertEqual(unauthorized.status_code, 401)
        self.assertNotIn("credential", unauthorized.text.casefold())

        schema = app.openapi()
        expected = {
            "/api/memorykeeper/auto-tags/status": {"get"},
            "/api/memorykeeper/auto-tags/failed": {"get"},
            "/api/memorykeeper/auto-tags/retry-failed": {"post"},
            "/api/memorykeeper/auto-tags/jobs/{job_id}/retry": {"post"},
            "/api/memorykeeper/auto-tags/curation-preview": {"get"},
        }
        for path, methods in expected.items():
            self.assertIn(path, schema["paths"])
            self.assertEqual(set(schema["paths"][path]), methods)
            for method in methods:
                self.assertEqual(
                    schema["paths"][path][method]["security"],
                    [{"TCBackendBearer": []}],
                )


if __name__ == "__main__":
    unittest.main()
