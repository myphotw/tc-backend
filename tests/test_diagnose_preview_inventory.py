from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.services.storage_service import StorageService
from scripts.diagnose_preview_inventory import (
    _oriented_size,
    _read_orientation,
    diagnose_preview_inventory,
)


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


def _database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)()


def _write_jpeg(
    path: Path,
    size: tuple[int, int],
    *,
    orientation: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = None
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
    image = Image.new("RGB", size, "navy")
    try:
        if exif is None:
            image.save(path, format="JPEG")
        else:
            image.save(path, format="JPEG", exif=exif)
    finally:
        image.close()


def _add_image(
    db,
    storage: LocalStorageService,
    number: int,
    *,
    original_size: tuple[int, int] | None,
    preview_size: tuple[int, int] | None,
    orientation: int | None = None,
    corrupt_original: bool = False,
    corrupt_preview: bool = False,
    preview_path_recorded: bool = True,
) -> CommonFile:
    digest = f"{number:064x}"
    original = storage.original_root / f"{digest}.jpg"
    preview = storage.preview_root / f"{digest}.jpg"
    if corrupt_original:
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_bytes(b"not-an-image")
    elif original_size is not None:
        _write_jpeg(original, original_size, orientation=orientation)
    if corrupt_preview:
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"not-an-image")
    elif preview_size is not None:
        _write_jpeg(preview, preview_size)

    common_file = CommonFile(
        file_id=digest,
        original_name=f"{number}.jpg",
        extension=".jpg",
        mime_type="image/jpeg",
        original_path=storage.to_relative_path(original),
        preview_path=(
            storage.to_relative_path(preview) if preview_path_recorded else None
        ),
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


def test_inventory_classifies_preview_states_and_percentages(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    _add_image(db, storage, 1, original_size=(3000, 100), preview_size=(2560, 85))
    _add_image(db, storage, 2, original_size=(1800, 100), preview_size=(1800, 100))
    _add_image(db, storage, 3, original_size=(3000, 100), preview_size=(1280, 43))
    _add_image(db, storage, 4, original_size=(3000, 100), preview_size=(2800, 93))
    _add_image(db, storage, 5, original_size=(3000, 100), preview_size=None)
    _add_image(db, storage, 6, original_size=None, preview_size=(2560, 85))
    _add_image(
        db,
        storage,
        7,
        original_size=None,
        preview_size=(2560, 85),
        corrupt_original=True,
    )
    _add_image(
        db,
        storage,
        8,
        original_size=(3000, 100),
        preview_size=None,
        corrupt_preview=True,
    )
    try:
        result = diagnose_preview_inventory(
            db,
            storage_service=storage,
            progress_every=100,
            progress_stream=None,
        )

        summary = result["summary"]
        assert summary["total_active_memorykeeper_images"] == 8
        assert summary["normal_preview"] == 2
        assert summary["outdated_low_resolution_preview"] == 1
        assert summary["preview_larger_than_expected"] == 1
        assert summary["preview_file_missing"] == 1
        assert summary["original_file_missing"] == 1
        assert summary["original_probe_or_decode_failed"] == 1
        assert summary["preview_probe_or_decode_failed"] == 1
        assert summary["unsupported_or_unclassified"] == 0
        assert summary["normal_percentage"] == 25.0
        assert summary["outdated_percentage"] == 12.5
        assert summary["error_percentage"] == 50.0
        assert summary["regeneration_candidate_count"] == 4

        jpeg = result["by_format"]["JPEG"]
        assert jpeg == {
            "total": 8,
            "normal": 2,
            "outdated": 1,
            "larger": 1,
            "missing": 2,
            "error": 2,
        }
    finally:
        db.close()
        engine.dispose()


def test_one_pixel_rounding_difference_is_normal(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    _add_image(db, storage, 20, original_size=(3000, 100), preview_size=(2559, 85))
    try:
        result = diagnose_preview_inventory(
            db,
            storage_service=storage,
            progress_stream=None,
        )

        assert result["summary"]["normal_preview"] == 1
        assert result["summary"]["outdated_low_resolution_preview"] == 0
    finally:
        db.close()
        engine.dispose()


def test_unknown_image_format_is_reported_as_unsupported(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _add_image(
        db,
        storage,
        21,
        original_size=None,
        preview_size=None,
        corrupt_original=True,
    )
    common_file.original_name = "vector.svg"
    common_file.extension = ".svg"
    common_file.mime_type = "image/svg+xml"
    db.commit()
    try:
        result = diagnose_preview_inventory(
            db,
            storage_service=storage,
            progress_stream=None,
        )

        assert result["summary"]["unsupported_or_unclassified"] == 1
        assert result["summary"]["original_probe_or_decode_failed"] == 0
        assert result["by_format"]["OTHER"]["error"] == 1
    finally:
        db.close()
        engine.dispose()


def test_orientation_metadata_matches_exif_transpose_dimensions(tmp_path: Path) -> None:
    source = tmp_path / "oriented.jpg"
    _write_jpeg(source, (600, 300), orientation=6)

    orientation = _read_orientation(source)
    with Image.open(source) as image:
        raw_size = image.size
        transposed = ImageOps.exif_transpose(image)
        try:
            expected = transposed.size
        finally:
            transposed.close()

    assert orientation == 6
    assert _oriented_size(raw_size, orientation=orientation) == expected == (300, 600)


def test_inventory_is_read_only_and_limits_samples(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    files = [
        _add_image(
            db,
            storage,
            number,
            original_size=(3000, 100),
            preview_size=(1280, 43),
        )
        for number in range(30, 33)
    ]
    before = {
        path: path.read_bytes()
        for path in tmp_path.rglob("*.jpg")
    }
    writes: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _many,
    ) -> None:
        verb = statement.lstrip().split(maxsplit=1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}:
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", capture_statement)
    progress = io.StringIO()
    try:
        result = diagnose_preview_inventory(
            db,
            storage_service=storage,
            progress_every=2,
            sample_limit=2,
            progress_stream=progress,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    try:
        assert writes == []
        assert {
            path: path.read_bytes()
            for path in tmp_path.rglob("*.jpg")
        } == before
        assert len(result["samples"]["outdated_low_resolution_preview"]) == 2
        assert len(progress.getvalue().splitlines()) == 2
        assert [sample["file_id"] for sample in result["samples"]["outdated_low_resolution_preview"]] == [
            files[0].file_id,
            files[1].file_id,
        ]
    finally:
        db.close()
        engine.dispose()


def test_limit_and_file_id_are_applied_before_file_reads(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    first = _add_image(
        db,
        storage,
        40,
        original_size=(3000, 100),
        preview_size=(2560, 85),
    )
    second = _add_image(
        db,
        storage,
        41,
        original_size=(3000, 100),
        preview_size=(1280, 43),
    )
    try:
        limited = diagnose_preview_inventory(
            db,
            storage_service=storage,
            limit=1,
            progress_stream=None,
        )
        selected = diagnose_preview_inventory(
            db,
            storage_service=storage,
            file_id=second.file_id,
            progress_stream=None,
        )

        assert limited["summary"]["total_active_memorykeeper_images"] == 1
        assert limited["summary"]["normal_preview"] == 1
        assert selected["summary"]["total_active_memorykeeper_images"] == 1
        assert selected["summary"]["outdated_low_resolution_preview"] == 1
        assert first.file_id != second.file_id
    finally:
        db.close()
        engine.dispose()
