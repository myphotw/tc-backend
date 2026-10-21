from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.database import Base
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.metadata_history import CommonMetadataHistory
from app.common.models.upload_job import UploadJob, UploadSourceType
from app.common.repositories.metadata_priority import MetadataPriority
from app.common.repositories.metadata_repository import MetadataRepository, MetadataSource
from app.common.services.storage_service import StorageService
from scripts.repair_missing_gps_metadata import repair_missing_gps_metadata
from worker.plugins.base import PluginContext
from worker.plugins.exif_plugin import ExifPlugin
from worker.plugins.gps_plugin import GpsPlugin
from worker.plugins.hash_plugin import HashPlugin
from worker.plugins.metadata_plugin import MetadataPlugin
from worker.plugins.storage_plugin import StoragePlugin


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


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
    def mobile_original_root(self) -> Path:
        return self.root / "mobile-original"

    @property
    def preview_root(self) -> Path:
        return self.root / "preview"

    @property
    def thumb_root(self) -> Path:
        return self.root / "thumb"


def _history(
    file_id: int,
    field_name: str,
    new_value: object,
    *,
    source: str,
    priority: int,
) -> CommonMetadataHistory:
    return CommonMetadataHistory(
        file_id=file_id,
        field_name=field_name,
        old_value=None,
        new_value=None if new_value is None else str(new_value),
        source=source,
        priority=priority,
    )


def _file(db, suffix: str = "1") -> CommonFile:
    item = CommonFile(
        file_id=suffix * 64,
        original_name="photo.jpg",
        extension=".jpg",
        mime_type="image/jpeg",
        original_path=f"original/{suffix * 64}.jpg",
        service_name="MemoryKeeper",
        deleted=False,
    )
    db.add(item)
    db.flush()
    return item


def test_regular_priority_contract_still_blocks_equal_non_user_history(db_session):
    item = _file(db_session)
    db_session.add(_history(item.id, "gps_lat", 35.0, source="GPS", priority=80))
    db_session.commit()

    metadata = MetadataRepository(db_session).upsert_fields(
        file_id=item.id,
        values={"gps_lat": 36.0},
        source=MetadataSource.GPS,
    )

    assert metadata.gps_lat is None


def test_initial_gps_write_without_history_is_persisted(db_session):
    item = _file(db_session)
    db_session.commit()

    metadata = MetadataRepository(db_session).upsert_fields(
        file_id=item.id,
        values={"gps_lat": 36.0, "gps_lon": 128.0},
        source=MetadataSource.GPS,
    )

    assert (metadata.gps_lat, metadata.gps_lon) == (36.0, 128.0)


def test_restore_rebuilds_missing_non_user_projection(db_session):
    item = _file(db_session)
    db_session.add_all(
        [
            _history(item.id, "gps_lat", 35.0, source="GPS", priority=80),
            _history(item.id, "gps_lon", 127.0, source="GPS", priority=80),
        ]
    )
    db_session.commit()

    metadata = MetadataRepository(db_session).upsert_fields(
        file_id=item.id,
        values={"gps_lat": 36.0, "gps_lon": 128.0},
        source=MetadataSource.GPS,
        restore_missing_projection=True,
    )

    assert (metadata.gps_lat, metadata.gps_lon) == (36.0, 128.0)


def test_restore_replays_user_gps_value_instead_of_asset_value(db_session):
    item = _file(db_session)
    db_session.add_all(
        [
            _history(item.id, "gps_lat", 37.5, source="USER", priority=100),
            _history(item.id, "gps_lon", 126.9, source="USER", priority=100),
        ]
    )
    db_session.commit()

    metadata = MetadataRepository(db_session).upsert_fields(
        file_id=item.id,
        values={"gps_lat": 35.0, "gps_lon": 127.0},
        source=MetadataSource.EXIF,
        restore_missing_projection=True,
    )

    assert (metadata.gps_lat, metadata.gps_lon) == (37.5, 126.9)
    assert metadata.locked is True


def test_restore_preserves_explicit_user_gps_clear(db_session):
    item = _file(db_session)
    db_session.add_all(
        [
            _history(item.id, "gps_lat", None, source="USER", priority=100),
            _history(item.id, "gps_lon", None, source="USER", priority=100),
        ]
    )
    db_session.commit()

    metadata = MetadataRepository(db_session).upsert_fields(
        file_id=item.id,
        values={"gps_lat": 35.0, "gps_lon": 127.0},
        source=MetadataSource.EXIF,
        restore_missing_projection=True,
    )

    assert metadata.gps_lat is None
    assert metadata.gps_lon is None
    assert metadata.locked is True


def test_unrelated_user_history_does_not_block_gps_restore(db_session):
    item = _file(db_session)
    db_session.add_all(
        [
            _history(item.id, "country", "사용자 국가", source="USER", priority=100),
            _history(item.id, "gps_lat", 35.0, source="GPS", priority=80),
            _history(item.id, "gps_lon", 127.0, source="GPS", priority=80),
        ]
    )
    db_session.commit()

    metadata = MetadataRepository(db_session).upsert_fields(
        file_id=item.id,
        values={"gps_lat": 36.0, "gps_lon": 128.0},
        source=MetadataSource.GPS,
        restore_missing_projection=True,
    )

    assert (metadata.gps_lat, metadata.gps_lon) == (36.0, 128.0)
    assert metadata.country == "사용자 국가"
    assert metadata.locked is True


def test_restore_changes_roll_back_as_one_repository_unit(db_session):
    item = _file(db_session)
    db_session.commit()

    MetadataRepository(db_session).upsert_fields(
        file_id=item.id,
        values={"gps_lat": 36.0, "gps_lon": 128.0},
        source=MetadataSource.GPS,
        commit=False,
        restore_missing_projection=True,
    )
    db_session.rollback()

    assert (
        db_session.query(CommonFileMetadata)
        .filter(CommonFileMetadata.file_id == item.id)
        .first()
        is None
    )


def test_restore_without_gps_keeps_null_and_commits_exif_projection(db_session):
    item = _file(db_session)
    db_session.add(CommonFileService(file_id=item.id, service_name="MemoryKeeper"))
    db_session.commit()
    context = PluginContext(
        db=db_session,
        storage_service=object(),
        common_file=item,
        original_path=Path("restored-without-gps.jpg"),
        service_name="MemoryKeeper",
        restore_deleted_common_file=True,
    )

    with patch(
        "worker.plugins.exif_plugin.ExifReader.read",
        return_value={"camera_make": "restore camera"},
    ):
        MetadataPlugin().run(context)
        ExifPlugin().run(context)

    db_session.rollback()
    metadata = db_session.query(CommonFileMetadata).filter_by(file_id=item.id).one()
    assert metadata.camera_make == "restore camera"
    assert metadata.gps_lat is None
    assert metadata.gps_lon is None
    assert context.has_gps is False


def test_deleted_mobile_reupload_restores_gps_despite_retained_history(
    db_session,
    tmp_path,
    monkeypatch,
):
    storage = LocalStorageService(tmp_path / "PhotoPlatform")
    incoming = storage.incoming_root / "restored.jpg"
    incoming.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), color="navy").save(incoming, format="JPEG")
    digest = hashlib.sha256(incoming.read_bytes()).hexdigest()
    common_file = CommonFile(
        file_id=digest,
        original_name="deleted.jpg",
        service_name="MemoryKeeper",
        deleted=True,
    )
    job = UploadJob(
        job_id="restore-gps-job",
        source_type=UploadSourceType.UPLOAD_MOBILE,
        status="PROCESSING",
        incoming_path=storage.to_relative_path(incoming),
        service_name="MemoryKeeper",
        client_content_sha256=digest,
    )
    db_session.add_all([common_file, job])
    db_session.flush()
    db_session.add_all(
        [
            _history(common_file.id, "gps_lat", 34.0, source="GPS", priority=80),
            _history(common_file.id, "gps_lon", 135.0, source="GPS", priority=80),
        ]
    )
    db_session.commit()

    context = PluginContext(
        db=db_session,
        storage_service=storage,
        incoming_path=incoming,
        job=job,
        original_name="restored.jpg",
        extension=".jpg",
        mime_type="image/jpeg",
        width=32,
        height=24,
        service_name="MemoryKeeper",
    )
    monkeypatch.setattr(
        "worker.plugins.exif_plugin.ExifReader.read",
        lambda _self, _path: {
            "gps_lat": 34.6508576,
            "gps_lon": 135.510528,
            "image_width": 32,
            "image_height": 24,
        },
    )
    monkeypatch.setattr(
        "worker.plugins.gps_plugin.GeocodeCacheRepository.find",
        lambda _self, **_kwargs: SimpleNamespace(
            country="일본",
            province="Osaka",
            city="Osaka",
            district=None,
            place_name="복원 위치",
            latitude=34.6508576,
            longitude=135.510528,
        ),
    )
    monkeypatch.setattr(
        "worker.plugins.gps_plugin.MemoryKeeperPlaceService.auto_match_file",
        lambda _self, **_kwargs: False,
    )

    HashPlugin().run(context)
    StoragePlugin().run(context)
    MetadataPlugin().run(context)
    ExifPlugin().run(context)
    GpsPlugin().run(context)

    metadata = db_session.query(CommonFileMetadata).filter_by(file_id=common_file.id).one()
    assert context.restore_deleted_common_file is True
    assert common_file.deleted is False
    assert (metadata.gps_lat, metadata.gps_lon) == (34.6508576, 135.510528)
    assert "GPS_RESTORE_PERSISTED" in context.processing_log
    assert "MEMORYKEEPER_PLACE_UNMATCHED" in context.processing_log
    assert "GPS_COMPLETE" in context.processing_log


def test_restore_gps_enrichment_failure_rolls_back_plugin_changes(db_session):
    item = _file(db_session)
    original_path = Path("restored.jpg")
    db_session.add(CommonFileService(file_id=item.id, service_name="MemoryKeeper"))
    db_session.commit()
    history_count = db_session.query(CommonMetadataHistory).count()
    context = PluginContext(
        db=db_session,
        storage_service=object(),
        common_file=item,
        original_path=original_path,
        service_name="MemoryKeeper",
        restore_deleted_common_file=True,
    )
    cached = SimpleNamespace(
        country="대한민국",
        province="서울특별시",
        city="서울특별시",
        district="중구",
        place_name="복원 실패 위치",
        latitude=35.0,
        longitude=127.0,
    )

    with patch(
        "worker.plugins.exif_plugin.ExifReader.read",
        return_value={
            "camera_make": "restore camera",
            "gps_lat": 35.0,
            "gps_lon": 127.0,
        },
    ):
        MetadataPlugin().run(context)
        ExifPlugin().run(context)

    assert db_session.in_transaction()
    with patch(
        "worker.plugins.gps_plugin.GeocodeCacheRepository.find",
        return_value=cached,
    ), patch(
        "worker.plugins.gps_plugin.MemoryKeeperPlaceService.auto_match_file",
        side_effect=RuntimeError("place match failed"),
    ), pytest.raises(RuntimeError, match="place match failed"):
        GpsPlugin().run(context)

    assert (
        db_session.query(CommonFileMetadata)
        .filter(CommonFileMetadata.file_id == item.id)
        .first()
        is None
    )
    assert db_session.query(CommonMetadataHistory).count() == history_count
    assert any(log.startswith("GPS_FAILED:") for log in context.processing_log)


class FakeStorage:
    def __init__(self, path: Path) -> None:
        self.path = path

    def resolve_original_path(self, _stored_path: str) -> Path:
        return self.path


class FakeExifReader:
    def read(self, _path: Path) -> dict[str, float]:
        return {"gps_lat": 34.6508576, "gps_lon": 135.510528}


def _repair_target(db_session, tmp_path):
    original = tmp_path / "original.jpg"
    original.write_bytes(b"original")
    item = _file(db_session, "a")
    item.original_path = "original/a.jpg"
    metadata = CommonFileMetadata(file_id=item.id)
    db_session.add_all(
        [metadata, CommonFileService(file_id=item.id, service_name="MemoryKeeper")]
    )
    db_session.commit()
    return item, metadata, original


def test_single_file_repair_is_dry_run_by_default(db_session, tmp_path):
    item, metadata, original = _repair_target(db_session, tmp_path)
    before_history = db_session.query(CommonMetadataHistory).count()

    result = repair_missing_gps_metadata(
        db_session,
        storage_service=FakeStorage(original),
        common_file_id=item.id,
        expected_sha256=item.file_id,
        exif_reader=FakeExifReader(),
    )

    db_session.refresh(metadata)
    assert result.status == "WOULD_REPAIR"
    assert metadata.gps_lat is None
    assert metadata.gps_lon is None
    assert db_session.query(CommonMetadataHistory).count() == before_history


def test_single_file_repair_rejects_id_sha_mismatch(db_session, tmp_path):
    item, metadata, original = _repair_target(db_session, tmp_path)

    with pytest.raises(ValueError, match="ID and SHA-256 do not match"):
        repair_missing_gps_metadata(
            db_session,
            storage_service=FakeStorage(original),
            common_file_id=item.id,
            expected_sha256="b" * 64,
            exif_reader=FakeExifReader(),
            execute=True,
        )

    db_session.refresh(metadata)
    assert metadata.gps_lat is None
    assert metadata.gps_lon is None


def test_single_file_repair_updates_only_exact_target(db_session, tmp_path):
    item, metadata, original = _repair_target(db_session, tmp_path)
    other = _file(db_session, "b")
    other_metadata = CommonFileMetadata(file_id=other.id)
    db_session.add(other_metadata)
    db_session.commit()

    result = repair_missing_gps_metadata(
        db_session,
        storage_service=FakeStorage(original),
        common_file_id=item.id,
        expected_sha256=item.file_id,
        exif_reader=FakeExifReader(),
        execute=True,
    )

    db_session.refresh(metadata)
    db_session.refresh(other_metadata)
    assert result.status == "REPAIRED"
    assert (metadata.gps_lat, metadata.gps_lon) == (34.6508576, 135.510528)
    assert other_metadata.gps_lat is None
    assert other_metadata.gps_lon is None
    histories = (
        db_session.query(CommonMetadataHistory)
        .filter(CommonMetadataHistory.file_id == item.id)
        .filter(CommonMetadataHistory.field_name.in_(["gps_lat", "gps_lon"]))
        .all()
    )
    assert {row.source for row in histories} == {MetadataSource.GPS}
    assert {row.priority for row in histories} == {int(MetadataPriority.GPS)}


def test_single_file_repair_rejects_latest_user_gps_history(db_session, tmp_path):
    item, metadata, original = _repair_target(db_session, tmp_path)
    db_session.add_all(
        [
            _history(item.id, "gps_lat", None, source="USER", priority=100),
            _history(item.id, "gps_lon", None, source="USER", priority=100),
        ]
    )
    db_session.commit()

    try:
        repair_missing_gps_metadata(
            db_session,
            storage_service=FakeStorage(original),
            common_file_id=item.id,
            expected_sha256=item.file_id,
            exif_reader=FakeExifReader(),
            execute=True,
        )
    except ValueError as exc:
        assert "USER GPS history" in str(exc)
    else:
        raise AssertionError("USER GPS history must block automatic repair")

    db_session.refresh(metadata)
    assert metadata.gps_lat is None
    assert metadata.gps_lon is None
