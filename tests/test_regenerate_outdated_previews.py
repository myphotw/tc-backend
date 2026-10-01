from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.services.media_probe import MediaCategory, MediaProbeResult
from app.common.services.storage_service import StorageService
from scripts.regenerate_outdated_previews import regenerate_outdated_previews


class LocalStorageService(StorageService):
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def storage_root(self) -> Path:
        return self.root

    @property
    def original_root(self) -> Path:
        return self.root / "original"

    @property
    def preview_root(self) -> Path:
        return self.root / "preview"

    @property
    def thumb_root(self) -> Path:
        return self.root / "thumb"


class HeicProbe:
    def probe(self, _path, *, filename):
        assert filename.endswith(".heic")
        return MediaProbeResult(
            MediaCategory.HEIC,
            ".heic",
            "image/heic",
            width=960,
            height=480,
        )


def _database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)()


def _write_jpeg(
    path: Path,
    size: tuple[int, int],
    *,
    orientation: int | None = None,
    color: str = "navy",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = None
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
    image = Image.new("RGB", size, color)
    try:
        if exif is None:
            image.save(path, format="JPEG")
        else:
            image.save(path, format="JPEG", exif=exif)
    finally:
        image.close()


def _write_mpo(path: Path, size: tuple[int, int] = (960, 480)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    first = Image.new("RGB", size, "red")
    second = Image.new("RGB", size, "blue")
    try:
        first.save(path, format="MPO", save_all=True, append_images=[second])
    finally:
        first.close()
        second.close()


def _add_image(
    db,
    storage: LocalStorageService,
    number: int,
    *,
    original_size: tuple[int, int] = (3000, 100),
    preview_size: tuple[int, int] = (2048, 68),
    orientation: int | None = None,
    original_suffix: str = ".jpg",
    mime_type: str = "image/jpeg",
    write_original=None,
) -> CommonFile:
    digest = f"{number:064x}"
    original = storage.original_root / f"{digest}{original_suffix}"
    preview = storage.preview_root / f"{digest}.jpg"
    thumb = storage.thumb_root / f"{digest}.jpg"
    if write_original is None:
        _write_jpeg(original, original_size, orientation=orientation)
    else:
        write_original(original)
    _write_jpeg(preview, preview_size, color="gray")
    _write_jpeg(thumb, (160, 80), color="green")
    common_file = CommonFile(
        file_id=digest,
        original_name=f"source-{number}{original_suffix}",
        extension=original_suffix,
        mime_type=mime_type,
        original_path=storage.to_relative_path(original),
        preview_path=storage.to_relative_path(preview),
        thumb_path=storage.to_relative_path(thumb),
        deleted=False,
    )
    db.add(common_file)
    db.flush()
    db.add(
        CommonFileService(
            file_id=common_file.id,
            service_name="MemoryKeeper",
        )
    )
    db.commit()
    return common_file


def _paths(storage: LocalStorageService, common_file: CommonFile):
    return (
        storage.resolve_storage_path(common_file.original_path),
        storage.resolve_storage_path(common_file.preview_path),
        storage.resolve_storage_path(common_file.thumb_path),
    )


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        image.load()
        return image.size


def _staging_files(storage: LocalStorageService) -> list[Path]:
    return list(storage.preview_root.rglob(".*preview-regeneration*"))


def test_execute_is_preview_only_atomic_and_resumable(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _add_image(db, storage, 1)
    original, preview, thumb = _paths(storage, common_file)
    original_before = original.read_bytes()
    preview_before = preview.read_bytes()
    thumb_before = thumb.read_bytes()
    persisted_preview_path = common_file.preview_path
    try:
        first = regenerate_outdated_previews(
            db,
            storage_service=storage,
            execute=True,
            progress_stream=None,
        )

        assert first["candidates"] == 1
        assert first["regenerated"] == 1
        assert max(_image_size(preview)) == 2560
        assert preview.read_bytes() != preview_before
        assert original.read_bytes() == original_before
        assert thumb.read_bytes() == thumb_before
        assert db.get(CommonFile, common_file.id).preview_path == persisted_preview_path
        assert _staging_files(storage) == []

        completed_bytes = preview.read_bytes()
        second = regenerate_outdated_previews(
            db,
            storage_service=storage,
            execute=True,
            progress_stream=None,
        )
        assert second["candidates"] == 0
        assert second["regenerated"] == 0
        assert second["skipped_normal"] == 1
        assert preview.read_bytes() == completed_bytes
    finally:
        db.close()
        engine.dispose()

def test_current_preview_is_skipped_byte_for_byte(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _add_image(db, storage, 2, preview_size=(2560, 85))
    _original, preview, _thumb = _paths(storage, common_file)
    before = preview.read_bytes()
    try:
        result = regenerate_outdated_previews(
            db,
            storage_service=storage,
            execute=True,
            progress_stream=None,
        )
        assert result["skipped_normal"] == 1
        assert result["candidates"] == 0
        assert preview.read_bytes() == before
    finally:
        db.close()
        engine.dispose()


def test_small_original_is_not_upscaled(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _add_image(
        db,
        storage,
        3,
        original_size=(1800, 100),
        preview_size=(1200, 67),
    )
    _original, preview, _thumb = _paths(storage, common_file)
    try:
        result = regenerate_outdated_previews(
            db,
            storage_service=storage,
            execute=True,
            progress_stream=None,
        )
        assert result["regenerated"] == 1
        assert _image_size(preview) == (1800, 100)
    finally:
        db.close()
        engine.dispose()


def test_exif_orientation_is_applied_before_resize(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _add_image(
        db,
        storage,
        4,
        orientation=6,
        preview_size=(68, 2048),
    )
    _original, preview, _thumb = _paths(storage, common_file)
    try:
        result = regenerate_outdated_previews(
            db,
            storage_service=storage,
            execute=True,
            progress_stream=None,
        )
        assert result["regenerated"] == 1
        width, height = _image_size(preview)
        assert height == 2560
        assert width < height
    finally:
        db.close()
        engine.dispose()


def test_mpo_uses_first_frame_for_jpeg_preview(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _add_image(
        db,
        storage,
        5,
        original_size=(960, 480),
        preview_size=(480, 240),
        write_original=_write_mpo,
    )
    _original, preview, _thumb = _paths(storage, common_file)
    try:
        result = regenerate_outdated_previews(
            db,
            storage_service=storage,
            execute=True,
            progress_stream=None,
        )
        assert result["regenerated"] == 1
        with Image.open(preview) as image:
            image.load()
            red, _green, blue = image.convert("RGB").getpixel((10, 10))
            assert image.format == "JPEG"
            assert image.size == (960, 480)
            assert red > blue
    finally:
        db.close()
        engine.dispose()


def test_heic_uses_existing_decoder_contract(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _add_image(
        db,
        storage,
        6,
        original_size=(960, 480),
        preview_size=(480, 240),
        original_suffix=".heic",
        mime_type="image/heic",
    )
    _original, preview, _thumb = _paths(storage, common_file)
    try:
        with patch(
            "scripts.regenerate_outdated_previews.register_heif_opener"
        ) as register:
            result = regenerate_outdated_previews(
                db,
                storage_service=storage,
                media_probe=HeicProbe(),
                execute=True,
                progress_stream=None,
            )
        assert result["regenerated"] == 1
        register.assert_called_once_with()
        assert _image_size(preview) == (960, 480)
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("patch_target", "exception"),
    [
        (
            "app.common.services.storage_service.StorageService._save_image",
            OSError("encode failed"),
        ),
        (
            "scripts.regenerate_outdated_previews._validate_staged_preview",
            ValueError("verify failed"),
        ),
        (
            "scripts.regenerate_outdated_previews._replace_staged_preview",
            OSError("replace failed"),
        ),
    ],
)
def test_failure_preserves_existing_preview_and_cleans_staging(
    tmp_path: Path,
    patch_target: str,
    exception: Exception,
) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _add_image(db, storage, 7)
    _original, preview, _thumb = _paths(storage, common_file)
    before = preview.read_bytes()
    try:
        with patch(patch_target, side_effect=exception):
            result = regenerate_outdated_previews(
                db,
                storage_service=storage,
                execute=True,
                progress_stream=None,
            )
        assert result["failed"] == 1
        assert result["regenerated"] == 0
        assert preview.read_bytes() == before
        assert _staging_files(storage) == []
    finally:
        db.close()
        engine.dispose()


def test_dry_run_limit_counts_candidates_and_writes_nothing(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    normal = _add_image(db, storage, 10, preview_size=(2560, 85))
    first = _add_image(db, storage, 11)
    second = _add_image(db, storage, 12)
    _add_image(db, storage, 13)
    before = {
        row.file_id: tuple(path.read_bytes() for path in _paths(storage, row))
        for row in (normal, first, second)
    }
    statements: list[str] = []

    def capture_writes(_conn, _cursor, statement, _parameters, _context, _many):
        verb = statement.lstrip().split(None, 1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"}:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_writes)
    try:
        result = regenerate_outdated_previews(
            db,
            storage_service=storage,
            limit=2,
            progress_stream=None,
        )
        assert result["mode"] == "dry-run"
        assert result["scanned"] == 3
        assert result["candidates"] == 2
        assert result["regenerated"] == 0
        assert statements == []
        for row in (normal, first, second):
            assert tuple(path.read_bytes() for path in _paths(storage, row)) == before[
                row.file_id
            ]
    finally:
        event.remove(engine, "before_cursor_execute", capture_writes)
        db.close()
        engine.dispose()


def test_file_id_selects_one_common_file(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    _add_image(db, storage, 20)
    selected = _add_image(db, storage, 21)
    try:
        result = regenerate_outdated_previews(
            db,
            storage_service=storage,
            file_id=selected.file_id,
            progress_stream=None,
        )
        assert result["selected_rows"] == 1
        assert result["scanned"] == 1
        assert result["candidates"] == 1
        assert result["file_id"] == selected.file_id
    finally:
        db.close()
        engine.dispose()


def test_stop_request_exits_without_touching_preview(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _add_image(db, storage, 22)
    _original, preview, _thumb = _paths(storage, common_file)
    before = preview.read_bytes()
    try:
        result = regenerate_outdated_previews(
            db,
            storage_service=storage,
            execute=True,
            stop_requested=lambda: True,
            progress_stream=None,
        )
        assert result["interrupted"] is True
        assert result["scanned"] == 0
        assert result["regenerated"] == 0
        assert preview.read_bytes() == before
    finally:
        db.close()
        engine.dispose()
