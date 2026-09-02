from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.vision_job import CommonVisionJob
from app.common.services.media_derivatives import MediaDerivativeResult
from app.common.services.media_probe import MediaCategory, MediaProbeResult
from app.common.services.storage_service import StorageService
from scripts.backfill_media_derivatives import backfill_media_derivatives


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


class FakeProbe:
    def __init__(self, category: MediaCategory) -> None:
        self.category = category

    def probe(self, _path, *, filename):
        del filename
        extension = ".heic" if self.category == MediaCategory.HEIC else ".mp4"
        mime = "image/heic" if self.category == MediaCategory.HEIC else "video/mp4"
        return MediaProbeResult(self.category, extension, mime, 640, 480)


class FakeDerivatives:
    def __init__(self, storage: LocalStorageService, *, fail: bool = False) -> None:
        self.storage = storage
        self.fail = fail
        self.calls = 0

    def generate(
        self,
        *,
        original_path,
        file_id,
        media,
        create_preview=True,
        create_thumbnail=True,
    ):
        del original_path
        self.calls += 1
        if self.fail:
            return MediaDerivativeResult(None, None, media.width, media.height, ("failed",))
        preview = None
        thumb = None
        if create_preview:
            preview = self.storage.build_derivative_path(
                kind="preview", file_id=file_id, extension=".jpg"
            )
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_bytes(b"preview")
        if create_thumbnail:
            thumb = self.storage.build_derivative_path(
                kind="thumb", file_id=file_id, extension=".jpg"
            )
            thumb.parent.mkdir(parents=True, exist_ok=True)
            thumb.write_bytes(b"thumb")
        return MediaDerivativeResult(preview, thumb, media.width, media.height)


def _database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)()


def _file(db, storage: LocalStorageService, *, shared: bool = False) -> CommonFile:
    digest = "d" * 64
    original = storage.original_root / "clip.mp4"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"video")
    common_file = CommonFile(
        file_id=digest,
        original_name="clip.mp4",
        extension=".mp4",
        mime_type="video/mp4",
        original_path=storage.to_relative_path(original),
        deleted=False,
    )
    db.add(common_file)
    db.flush()
    db.add(CommonFileService(file_id=common_file.id, service_name="MemoryKeeper"))
    if shared:
        db.add(CommonFileService(file_id=common_file.id, service_name="AstroJournal"))
    db.add(CommonFileMetadata(file_id=common_file.id, camera_make="keep"))
    db.add(CommonVisionJob(file_id=common_file.id, status="FAILED"))
    db.commit()
    return common_file


def test_dry_run_does_not_write_files_or_database(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _file(db, storage, shared=True)
    derivative = FakeDerivatives(storage)
    try:
        stats = backfill_media_derivatives(
            db,
            storage_service=storage,
            media_probe=FakeProbe(MediaCategory.VIDEO),
            derivative_service=derivative,
        )
        db.refresh(common_file)
        assert stats.eligible == 1 and stats.would_update == 1
        assert stats.shared_files == 1
        assert stats.existing_video_vision_jobs == 1
        assert derivative.calls == 0
        assert common_file.preview_path is None and common_file.thumb_path is None
        assert not storage.thumb_root.exists()
    finally:
        db.close()
        engine.dispose()


def test_video_execute_creates_only_thumb_and_is_idempotent(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _file(db, storage)
    derivative = FakeDerivatives(storage)
    original_path = common_file.original_path
    metadata = db.query(CommonFileMetadata).filter_by(file_id=common_file.id).one()
    try:
        first = backfill_media_derivatives(
            db,
            storage_service=storage,
            media_probe=FakeProbe(MediaCategory.VIDEO),
            derivative_service=derivative,
            execute=True,
        )
        second = backfill_media_derivatives(
            db,
            storage_service=storage,
            media_probe=FakeProbe(MediaCategory.VIDEO),
            derivative_service=derivative,
            execute=True,
        )
        db.refresh(common_file)
        db.refresh(metadata)
        assert first.updated == 1
        assert second.skipped_complete == 1
        assert derivative.calls == 1
        assert common_file.preview_path is None
        assert common_file.thumb_path.endswith(".jpg")
        assert common_file.original_path == original_path
        assert metadata.camera_make == "keep"
        assert db.query(CommonFileService).filter_by(file_id=common_file.id).count() == 1
        assert db.query(CommonVisionJob).filter_by(file_id=common_file.id).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_heic_execute_creates_preview_and_thumb(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _file(db, storage)
    common_file.original_name = "photo.heic"
    common_file.extension = ".heic"
    db.commit()
    try:
        stats = backfill_media_derivatives(
            db,
            storage_service=storage,
            media_probe=FakeProbe(MediaCategory.HEIC),
            derivative_service=FakeDerivatives(storage),
            execute=True,
            file_id=common_file.file_id,
        )
        db.refresh(common_file)
        assert stats.updated == 1
        assert common_file.preview_path.endswith(".jpg")
        assert common_file.thumb_path.endswith(".jpg")
    finally:
        db.close()
        engine.dispose()


def test_partial_failure_keeps_paths_null_and_unsafe_path_is_skipped(
    tmp_path: Path,
) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _file(db, storage)
    try:
        failed = backfill_media_derivatives(
            db,
            storage_service=storage,
            media_probe=FakeProbe(MediaCategory.VIDEO),
            derivative_service=FakeDerivatives(storage, fail=True),
            execute=True,
        )
        assert failed.failed == 1
        db.refresh(common_file)
        assert common_file.thumb_path is None

        common_file.original_path = str(tmp_path.parent / "outside.mp4")
        db.commit()
        skipped = backfill_media_derivatives(
            db,
            storage_service=storage,
            media_probe=FakeProbe(MediaCategory.VIDEO),
            derivative_service=FakeDerivatives(storage),
            execute=True,
        )
        assert skipped.skipped_unsafe_path == 1
    finally:
        db.close()
        engine.dispose()
