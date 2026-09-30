from __future__ import annotations

from pathlib import Path

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.file import CommonFile
from app.common.services.media_derivatives import MediaDerivativeResult
from app.common.services.media_probe import MediaCategory, MediaProbeResult
from app.common.services.storage_service import StorageService
from scripts.regenerate_media_derivatives import regenerate_media_derivatives


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
    def probe(self, _path, *, filename):
        if filename.endswith(".mp4"):
            return MediaProbeResult(MediaCategory.VIDEO, ".mp4", "video/mp4")
        return MediaProbeResult(MediaCategory.IMAGE, ".jpg", "image/jpeg")


class FakeDerivatives:
    def __init__(
        self,
        storage: LocalStorageService,
        *,
        fail_file_ids: set[str] | None = None,
    ) -> None:
        self.storage = storage
        self.fail_file_ids = fail_file_ids or set()
        self.calls: list[str] = []

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
        self.calls.append(file_id)
        if file_id in self.fail_file_ids:
            return MediaDerivativeResult(
                None,
                None,
                media.width,
                media.height,
                ("synthetic-failure",),
            )

        preview = None
        thumb = None
        if create_preview:
            preview = self.storage.build_derivative_path(
                kind="preview",
                file_id=file_id,
                extension=".jpg",
            )
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_bytes(b"new-preview")
        if create_thumbnail:
            extension = (
                ".jpg" if media.category == MediaCategory.VIDEO else media.extension
            )
            thumb = self.storage.build_derivative_path(
                kind="thumb",
                file_id=file_id,
                extension=extension,
            )
            thumb.parent.mkdir(parents=True, exist_ok=True)
            thumb.write_bytes(b"new-thumb")
        return MediaDerivativeResult(preview, thumb, media.width, media.height)


def _database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)()


def _file(
    db,
    storage: LocalStorageService,
    number: int,
    *,
    video: bool = False,
    existing_derivatives: bool = False,
    deleted: bool = False,
) -> CommonFile:
    digest = f"{number:064x}"
    extension = ".mp4" if video else ".jpg"
    original = storage.original_root / f"source-{number}{extension}"
    original.parent.mkdir(parents=True, exist_ok=True)
    if video:
        original.write_bytes(f"original-{number}".encode())
    else:
        Image.new("RGB", (960, 480), "navy").save(original, format="JPEG")
    common_file = CommonFile(
        file_id=digest,
        original_name=original.name,
        extension=extension,
        mime_type="video/mp4" if video else "image/jpeg",
        original_path=storage.to_relative_path(original),
        deleted=deleted,
    )
    if existing_derivatives:
        if not video:
            preview = storage.build_derivative_path(
                kind="preview",
                file_id=digest,
                extension=".jpg",
            )
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_bytes(b"old-preview")
            common_file.preview_path = storage.to_relative_path(preview)
        thumb = storage.build_derivative_path(
            kind="thumb",
            file_id=digest,
            extension=".jpg",
        )
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"old-thumb")
        common_file.thumb_path = storage.to_relative_path(thumb)
    db.add(common_file)
    db.commit()
    return common_file


def test_dry_run_reports_all_active_common_media_without_writing(
    tmp_path: Path,
) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    image = _file(db, storage, 1, existing_derivatives=True)
    video = _file(db, storage, 2, video=True, existing_derivatives=True)
    _file(db, storage, 3, deleted=True)
    db.add(
        CommonFile(
            file_id="f" * 64,
            original_name="missing.jpg",
            extension=".jpg",
            mime_type="image/jpeg",
            original_path=None,
            deleted=False,
        )
    )
    db.commit()
    derivatives = FakeDerivatives(storage)
    image_preview = storage.resolve_storage_path(image.preview_path)
    video_thumb = storage.resolve_storage_path(video.thumb_path)
    try:
        stats = regenerate_media_derivatives(
            db,
            storage_service=storage,
            media_probe=FakeProbe(),
            derivative_service=derivatives,
        )

        assert stats.scanned == 3
        assert stats.target_files == 2
        assert stats.image_targets == 1
        assert stats.video_targets == 1
        assert stats.preview_regeneration_targets == 1
        assert stats.thumbnail_regeneration_targets == 2
        assert stats.missing_originals == 1
        assert stats.potential_errors == 1
        assert stats.regenerated_files == 0
        assert derivatives.calls == []
        assert image_preview.read_bytes() == b"old-preview"
        assert video_thumb.read_bytes() == b"old-thumb"
    finally:
        db.close()
        engine.dispose()


def test_execute_replaces_existing_derivatives_without_changing_original(
    tmp_path: Path,
) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _file(db, storage, 4, existing_derivatives=True)
    original = storage.resolve_storage_path(common_file.original_path)
    original_before = original.read_bytes()
    preview_path_before = common_file.preview_path
    thumb_path_before = common_file.thumb_path
    try:
        stats = regenerate_media_derivatives(
            db,
            storage_service=storage,
            execute=True,
        )
        second = regenerate_media_derivatives(
            db,
            storage_service=storage,
            execute=True,
        )

        db.refresh(common_file)
        assert stats.regenerated_files == 1
        assert second.regenerated_files == 1
        assert stats.db_path_updates == 0
        assert second.db_path_updates == 0
        assert common_file.preview_path == preview_path_before
        assert common_file.thumb_path == thumb_path_before
        preview = storage.resolve_storage_path(common_file.preview_path)
        thumbnail = storage.resolve_storage_path(common_file.thumb_path)
        assert preview.read_bytes() != b"old-preview"
        assert thumbnail.read_bytes() != b"old-thumb"
        with Image.open(thumbnail) as image:
            assert image.size == (480, 240)
        assert not list(preview.parent.glob(".*.jpg"))
        assert not list(thumbnail.parent.glob(".*.jpg"))
        assert original.read_bytes() == original_before
    finally:
        db.close()
        engine.dispose()


def test_execute_creates_missing_derivatives_and_persists_paths(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _file(db, storage, 5)
    try:
        stats = regenerate_media_derivatives(
            db,
            storage_service=storage,
            media_probe=FakeProbe(),
            derivative_service=FakeDerivatives(storage),
            execute=True,
        )

        db.refresh(common_file)
        assert stats.regenerated_files == 1
        assert stats.db_path_updates == 1
        assert common_file.preview_path is not None
        assert common_file.thumb_path is not None
        assert storage.resolve_storage_path(common_file.preview_path).is_file()
        assert storage.resolve_storage_path(common_file.thumb_path).is_file()
    finally:
        db.close()
        engine.dispose()


def test_one_file_failure_is_reported_and_does_not_stop_the_batch(
    tmp_path: Path,
) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    failed_file = _file(db, storage, 6)
    successful_file = _file(db, storage, 7)
    derivatives = FakeDerivatives(storage, fail_file_ids={failed_file.file_id})
    try:
        stats = regenerate_media_derivatives(
            db,
            storage_service=storage,
            media_probe=FakeProbe(),
            derivative_service=derivatives,
            execute=True,
        )

        assert stats.failed_files == 1
        assert stats.regenerated_files == 1
        assert derivatives.calls == [failed_file.file_id, successful_file.file_id]
        assert any(failed_file.file_id in failure for failure in stats.failures)
    finally:
        db.close()
        engine.dispose()


def test_limit_caps_the_number_of_database_rows_scanned(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    _file(db, storage, 8)
    _file(db, storage, 9)
    try:
        stats = regenerate_media_derivatives(
            db,
            storage_service=storage,
            media_probe=FakeProbe(),
            derivative_service=FakeDerivatives(storage),
            limit=1,
        )

        assert stats.scanned == 1
        assert stats.target_files == 1
    finally:
        db.close()
        engine.dispose()
