from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.upload_job import UploadJob, UploadSourceType
from app.common.routers import upload as upload_router
from app.common.schemas.upload import UploadPreflightRequest
from app.common.services.storage_service import StorageService


class LocalIncomingStorage(StorageService):
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def storage_root(self) -> Path:
        return self.root

    @property
    def incoming_root(self) -> Path:
        return self.root / "incoming"


class MobileUploadPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.storage = LocalIncomingStorage(Path(self.temp.name) / "PhotoPlatform")
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    def _file(
        self,
        digest: str,
        *,
        services: tuple[str, ...],
        deleted: bool = False,
        original_path: str = "original/private.jpg",
    ) -> CommonFile:
        item = CommonFile(
            file_id=digest,
            original_name="private-name.jpg",
            original_path=original_path,
            deleted=deleted,
        )
        self.db.add(item)
        self.db.flush()
        self.db.add_all(
            CommonFileService(file_id=item.id, service_name=service)
            for service in services
        )
        self.db.commit()
        return item

    @staticmethod
    def _payload(digest: str) -> UploadPreflightRequest:
        return UploadPreflightRequest(
            service_name="MemoryKeeper",
            upload_source="mobile",
            client_content_sha256=digest,
        )

    def test_memorykeeper_pc_or_mobile_membership_is_duplicate(self) -> None:
        cases = (
            ("a" * 64, "original/pc-photo.jpg"),
            ("1" * 64, "mobile-original/mobile-photo.jpg"),
        )
        for digest, original_path in cases:
            self._file(
                digest,
                services=("MemoryKeeper",),
                original_path=original_path,
            )

        for digest, _ in cases:
            with self.subTest(digest=digest[0]):
                response = upload_router.preflight_upload(
                    self._payload(digest),
                    self.db,
                )
                self.assertEqual(
                    response.model_dump(),
                    {
                        "duplicate": True,
                        "upload_allowed": False,
                        "reason": "DUPLICATE_CONTENT",
                    },
                )

    def test_astro_only_new_and_tombstoned_files_are_not_duplicates(self) -> None:
        astro_digest = "b" * 64
        tombstone_digest = "c" * 64
        self._file(astro_digest, services=("AstroJournal",))
        self._file(
            tombstone_digest,
            services=("MemoryKeeper",),
            deleted=True,
        )

        for digest in (astro_digest, tombstone_digest, "d" * 64):
            with self.subTest(digest=digest[0]):
                response = upload_router.preflight_upload(
                    self._payload(digest),
                    self.db,
                )
                self.assertEqual(
                    response.model_dump(),
                    {
                        "duplicate": False,
                        "upload_allowed": True,
                        "reason": None,
                    },
                )

    def test_preflight_contract_rejects_invalid_scope_and_sha(self) -> None:
        invalid_payloads = (
            {
                "service_name": "AstroJournal",
                "upload_source": "mobile",
                "client_content_sha256": "a" * 64,
            },
            {
                "service_name": "MemoryKeeper",
                "upload_source": "desktop",
                "client_content_sha256": "a" * 64,
            },
            {
                "service_name": "MemoryKeeper",
                "upload_source": "mobile",
                "client_content_sha256": "A" * 64,
            },
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    UploadPreflightRequest.model_validate(payload)

    def test_upload_rejects_known_mobile_duplicate_before_incoming_or_job(self) -> None:
        digest = "e" * 64
        self._file(digest, services=("MemoryKeeper",))

        with patch.object(upload_router, "storage_service", self.storage):
            with self.assertRaises(HTTPException) as raised:
                upload_router.upload_file(
                    file=UploadFile(filename="secret.jpg", file=io.BytesIO(b"content")),
                    service_name="MemoryKeeper",
                    upload_source="mobile",
                    client_file_id="new-mobile-id",
                    client_content_sha256=digest,
                    db=self.db,
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail,
            {
                "code": "DUPLICATE_CONTENT",
                "message": "The file is already registered in MemoryKeeper.",
            },
        )
        self.assertEqual(self.db.query(UploadJob).count(), 0)
        self.assertFalse(self.storage.incoming_root.exists())

    def test_client_file_id_replay_precedes_duplicate_preflight(self) -> None:
        digest = "f" * 64
        self._file(digest, services=("MemoryKeeper",))
        existing = UploadJob(
            job_id="11111111-1111-1111-1111-111111111111",
            source_type=UploadSourceType.UPLOAD_MOBILE,
            status="COMPLETED",
            incoming_path="incoming/already-consumed.jpg",
            service_name="MemoryKeeper",
            client_file_id="stable-mobile-id",
            client_content_sha256=digest,
            file_id=digest,
        )
        self.db.add(existing)
        self.db.commit()

        with patch.object(upload_router, "storage_service", self.storage):
            response = upload_router.upload_file(
                file=UploadFile(filename="secret.jpg", file=io.BytesIO(b"content")),
                service_name="MemoryKeeper",
                upload_source="mobile",
                client_file_id="stable-mobile-id",
                client_content_sha256=digest,
                db=self.db,
            )

        self.assertTrue(response["idempotent_replay"])
        self.assertEqual(response["job_id"], existing.job_id)
        self.assertEqual(self.db.query(UploadJob).count(), 1)
        self.assertFalse(self.storage.incoming_root.exists())
