from __future__ import annotations

import warnings
from pathlib import Path

from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.services.media_probe import (
    MediaCategory,
    MediaProbeError,
    MediaProbeResult,
    UnsupportedMediaError,
)
from app.common.services.storage_service import StorageService
from scripts.diagnose_media_probe_failures import diagnose_media_probe_failures


class LocalStorageService(StorageService):
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def storage_root(self) -> Path:
        return self.root

    @property
    def original_root(self) -> Path:
        return self.root / "original"


class FakeProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def probe(self, _path: Path, *, filename: str) -> MediaProbeResult:
        self.calls.append(filename)
        if filename.startswith("unsupported"):
            raise UnsupportedMediaError("unsupported media format")
        if filename.startswith("probe-error"):
            raise MediaProbeError("synthetic probe failure")
        if filename.startswith("bomb-error"):
            raise Image.DecompressionBombError("unsafe dimensions")
        if filename.startswith("unexpected"):
            raise ValueError("must not be exposed")
        if filename.startswith("warning"):
            warnings.warn("large image", Image.DecompressionBombWarning)
        return MediaProbeResult(MediaCategory.IMAGE, ".jpg", "image/jpeg")


def _database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)()


def _add_file(
    db,
    storage: LocalStorageService,
    number: int,
    *,
    name: str,
    services: tuple[str, ...] = (),
) -> CommonFile:
    digest = f"{number:064x}"
    original = storage.original_root / f"{number}.bin"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"probe-input")
    common_file = CommonFile(
        file_id=digest,
        original_name=name,
        extension=Path(name).suffix or None,
        mime_type="application/octet-stream",
        original_path=storage.to_relative_path(original),
        deleted=False,
    )
    db.add(common_file)
    db.flush()
    for service_name in services:
        db.add(
            CommonFileService(
                file_id=common_file.id,
                service_name=service_name,
            )
        )
    db.commit()
    return common_file


def _group(result: dict[str, object], *, exception_class: str) -> dict[str, object]:
    groups = result["probe_failure_groups"]
    return next(
        group for group in groups if group["exception_class"] == exception_class
    )


def test_reports_image_success_and_unsupported_failure_by_reason_and_service(
    tmp_path: Path,
) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    _add_file(db, storage, 1, name="image.jpg", services=("MemoryKeeper",))
    failed = _add_file(
        db,
        storage,
        2,
        name="unsupported.raw",
        services=("AstroJournal",),
    )
    try:
        result = diagnose_media_probe_failures(
            db,
            storage_service=storage,
            media_probe=FakeProbe(),
        )

        assert result["scanned"] == 2
        assert result["probe_success"] == 1
        assert result["probe_failed"] == 1
        assert result["probe_success_by_category"] == {"IMAGE": 1}
        assert result["probe_failure_by_exception"] == {
            "UnsupportedMediaError": 1
        }
        assert result["probe_failure_by_reason"] == {
            "unsupported media format": 1
        }
        assert result["probe_failure_by_service"] == {"AstroJournal": 1}
        group = _group(result, exception_class="UnsupportedMediaError")
        assert group["sample_file_ids"] == [failed.file_id]
    finally:
        db.close()
        engine.dispose()


def test_other_probe_error_and_unexpected_exception_are_isolated(
    tmp_path: Path,
) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    _add_file(db, storage, 3, name="probe-error.dat")
    _add_file(db, storage, 4, name="unexpected.dat")
    _add_file(db, storage, 5, name="image.jpg")
    try:
        result = diagnose_media_probe_failures(
            db,
            storage_service=storage,
            media_probe=FakeProbe(),
        )

        assert result["probe_success"] == 1
        assert result["probe_failed"] == 2
        assert result["probe_failure_by_exception"] == {
            "MediaProbeError": 1,
            "ValueError": 1,
        }
        assert result["probe_failure_by_reason"] == {
            "synthetic probe failure": 1,
            "unexpected exception": 1,
        }
        assert result["probe_failure_by_service"] == {"unlinked": 2}
    finally:
        db.close()
        engine.dispose()


def test_multi_service_file_is_probed_once_and_projected_to_each_service(
    tmp_path: Path,
) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    _add_file(
        db,
        storage,
        6,
        name="unsupported.raw",
        services=("MemoryKeeper", "AstroJournal"),
    )
    probe = FakeProbe()
    try:
        result = diagnose_media_probe_failures(
            db,
            storage_service=storage,
            media_probe=probe,
        )

        assert result["scanned"] == 1
        assert result["probe_failed"] == 1
        assert result["probe_failure_by_service"] == {
            "AstroJournal": 1,
            "MemoryKeeper": 1,
        }
        assert len(result["probe_failure_groups"]) == 2
        assert probe.calls == ["unsupported.raw"]
    finally:
        db.close()
        engine.dispose()


def test_failure_group_samples_are_limited_to_three(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    files = [
        _add_file(
            db,
            storage,
            number,
            name="unsupported.raw",
            services=("MemoryKeeper",),
        )
        for number in range(7, 11)
    ]
    try:
        result = diagnose_media_probe_failures(
            db,
            storage_service=storage,
            media_probe=FakeProbe(),
        )

        group = _group(result, exception_class="UnsupportedMediaError")
        assert group["count"] == 4
        assert group["sample_file_ids"] == [item.file_id for item in files[:3]]
    finally:
        db.close()
        engine.dispose()


def test_decompression_bomb_warning_is_counted_without_disabling_warning(
    tmp_path: Path,
) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    common_file = _add_file(db, storage, 11, name="warning.jpg")
    original_limit = Image.MAX_IMAGE_PIXELS
    try:
        result = diagnose_media_probe_failures(
            db,
            storage_service=storage,
            media_probe=FakeProbe(),
        )

        assert result["probe_success"] == 1
        assert result["decompression_bomb_warnings"] == {
            "count": 1,
            "sample_file_ids": [common_file.file_id],
        }
        assert Image.MAX_IMAGE_PIXELS == original_limit
    finally:
        db.close()
        engine.dispose()


def test_decompression_bomb_error_isolated_as_image_decode_failure(
    tmp_path: Path,
) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    _add_file(db, storage, 14, name="bomb-error.jpg")
    _add_file(db, storage, 15, name="image.jpg")
    try:
        result = diagnose_media_probe_failures(
            db,
            storage_service=storage,
            media_probe=FakeProbe(),
        )

        assert result["probe_success"] == 1
        assert result["probe_failed"] == 1
        assert result["probe_failure_by_diagnostic_category"] == {
            "IMAGE_DECODE_FAILURE": 1
        }
        assert result["probe_failure_by_reason"] == {
            "image exceeds Pillow decompression-bomb safety limit": 1
        }
    finally:
        db.close()
        engine.dispose()


def test_limit_and_file_id_filter_apply_before_probe(tmp_path: Path) -> None:
    engine, db = _database()
    storage = LocalStorageService(tmp_path)
    first = _add_file(db, storage, 12, name="image.jpg")
    second = _add_file(db, storage, 13, name="unsupported.raw")
    try:
        limited = diagnose_media_probe_failures(
            db,
            storage_service=storage,
            media_probe=FakeProbe(),
            limit=1,
        )
        selected = diagnose_media_probe_failures(
            db,
            storage_service=storage,
            media_probe=FakeProbe(),
            file_id=second.file_id,
        )

        assert limited["scanned"] == 1
        assert limited["probe_success"] == 1
        assert limited["filters"] == {"limit": 1, "file_id": None}
        assert selected["scanned"] == 1
        assert selected["probe_failed"] == 1
        assert selected["filters"] == {"limit": None, "file_id": second.file_id}
        assert first.file_id != second.file_id
    finally:
        db.close()
        engine.dispose()
