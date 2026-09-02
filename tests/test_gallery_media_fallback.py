from __future__ import annotations

from pathlib import Path
import tempfile

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.file import CommonFile
from app.common.services.gallery_service import GalleryService
from app.common.services.storage_service import StorageService


class FakeStorageService:
    def __init__(self, paths: dict[str, Path]) -> None:
        self.paths = paths

    def resolve_storage_path(self, value: str) -> Path:
        return self.paths[value]


class TestGalleryMediaFallback:
    def setup_method(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()

    def teardown_method(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _file(self) -> CommonFile:
        common_file = CommonFile(
            file_id="a" * 64,
            original_name="media.jpg",
            original_path="original/a.jpg",
            preview_path="preview/a.jpg",
            thumb_path="thumb/a.jpg",
            deleted=False,
        )
        self.db.add(common_file)
        self.db.commit()
        return common_file

    def test_thumbnail_uses_persisted_preview_when_thumbnail_file_is_missing(
        self,
    ) -> None:
        common_file = self._file()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing_thumb = root / "missing-thumb.jpg"
            preview = root / "preview.jpg"
            preview.write_bytes(b"bounded-preview")
            service = GalleryService(self.db)
            service.storage_service = FakeStorageService(
                {
                    "thumb/a.jpg": missing_thumb,
                    "preview/a.jpg": preview,
                }
            )
            statements: list[str] = []

            def capture(
                _connection,
                _cursor,
                statement,
                _parameters,
                _context,
                _many,
            ) -> None:
                statements.append(statement)

            event.listen(self.engine, "before_cursor_execute", capture)
            try:
                path, media_type = service.get_media(
                    file_id=common_file.file_id,
                    kind="thumbnail",
                )
            finally:
                event.remove(self.engine, "before_cursor_execute", capture)

        assert path == preview
        assert media_type == "image/jpeg"
        assert len(statements) == 1

    def test_thumbnail_prefers_existing_thumbnail_over_preview(self) -> None:
        common_file = self._file()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thumbnail = root / "thumbnail.jpg"
            preview = root / "preview.jpg"
            thumbnail.write_bytes(b"thumbnail")
            preview.write_bytes(b"preview")
            service = GalleryService(self.db)
            service.storage_service = FakeStorageService(
                {
                    "thumb/a.jpg": thumbnail,
                    "preview/a.jpg": preview,
                }
            )

            path, media_type = service.get_media(
                file_id=common_file.file_id,
                kind="thumbnail",
            )

        assert path == thumbnail
        assert media_type == "image/jpeg"

    def test_thumbnail_stays_404_when_bounded_derivatives_are_both_missing(
        self,
    ) -> None:
        common_file = self._file()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = GalleryService(self.db)
            service.storage_service = FakeStorageService(
                {
                    "thumb/a.jpg": root / "missing-thumb.jpg",
                    "preview/a.jpg": root / "missing-preview.jpg",
                }
            )

            with pytest.raises(HTTPException) as failure:
                service.get_media(
                    file_id=common_file.file_id,
                    kind="thumbnail",
                )

        assert failure.value.status_code == 404


def test_preview_and_thumbnail_resize_preserve_landscape_and_portrait_ratio() -> None:
    storage = StorageService()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        landscape = storage._create_resized_image(
            image=Image.new("RGB", (800, 400)),
            target_root=root / "landscape",
            sha256="b" * 64,
            extension=".jpg",
            max_size=storage.THUMB_MAX_SIZE,
        )
        portrait = storage._create_resized_image(
            image=Image.new("RGB", (400, 800)),
            target_root=root / "portrait",
            sha256="c" * 64,
            extension=".jpg",
            max_size=storage.THUMB_MAX_SIZE,
        )

        with Image.open(landscape) as landscape_image:
            assert landscape_image.size == (400, 200)
        with Image.open(portrait) as portrait_image:
            assert portrait_image.size == (200, 400)
