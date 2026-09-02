from __future__ import annotations

import io
import json
from email.header import Header
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common.model_registry import Base
from app.common.models.upload_job import UploadJob
from app.common.models.file import CommonFile
from app.common.models.vision_job import CommonVisionJob
from app.common.services.media_derivatives import MediaDerivativeResult
from app.common.routers import upload as upload_router
from app.common.services.media_derivatives import MediaDerivativeService
from app.common.services.media_probe import (
    BoundedMediaCommandRunner,
    MediaCategory,
    MediaCommandResult,
    MediaCommandTimeout,
    MediaProbe,
    MediaProbeResult,
    UnsupportedMediaError,
)
from app.common.services.storage_service import StorageService
from app.common.services.upload_filename import decode_upload_filename
from worker import background_worker
from worker.plugins.base import PluginContext
from worker.plugins.vision_plugin import VisionPlugin


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


class ProbeRunner:
    def __init__(self, *, mov: bool = False, rotation: int = 0) -> None:
        self.mov = mov
        self.rotation = rotation

    def run(self, arguments, *, timeout_seconds):
        del arguments, timeout_seconds
        return MediaCommandResult(
            0,
            json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "width": 1920,
                            "height": 1080,
                            "tags": {"rotate": str(self.rotation)},
                        }
                    ],
                    "format": {"format_name": "mov,mp4", "duration": "12.5"},
                }
            ),
            "",
        )


class ThumbnailRunner:
    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        self.calls: list[list[str]] = []

    def run(self, arguments, *, timeout_seconds):
        self.calls.append(list(arguments))
        assert timeout_seconds > 0
        if self.timeout:
            raise MediaCommandTimeout("timeout")
        Image.new("RGB", (320, 180), "black").save(arguments[-1], format="JPEG")
        return MediaCommandResult(0, "", "")


def _video_file(path: Path, brand: bytes = b"isom") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 24)


def test_filename_decoder_restores_rfc2047_suffix_and_sanitizer_stays_safe(
    tmp_path: Path,
) -> None:
    encoded = Header("동영상.mp4", "utf-8").encode()
    assert decode_upload_filename(encoded) == "동영상.mp4"
    storage = LocalStorageService(tmp_path)
    upload = UploadFile(filename=f"../{encoded}", file=io.BytesIO(b"data"))
    saved = storage.save_incoming(upload, "job")
    assert Path(saved).name.endswith("동영상.mp4")
    assert ".." not in Path(saved).name


def test_image_probe_uses_bytes_over_false_suffix(tmp_path: Path) -> None:
    path = tmp_path / "movie.mp4"
    Image.new("RGB", (32, 24), "navy").save(path, format="JPEG")
    result = MediaProbe().probe(path, filename="movie.mp4")
    assert result.category == MediaCategory.IMAGE
    assert result.extension == ".jpg"
    assert result.mime_type == "image/jpeg"
    assert (result.width, result.height) == (32, 24)


@pytest.mark.parametrize(
    ("brand", "filename", "extension", "mime_type"),
    [
        (b"isom", "clip", ".mp4", "video/mp4"),
        (b"qt  ", "clip.jpg", ".mov", "video/quicktime"),
    ],
)
def test_video_probe_accepts_suffixless_and_mismatch(
    tmp_path: Path,
    brand: bytes,
    filename: str,
    extension: str,
    mime_type: str,
) -> None:
    path = tmp_path / "input"
    _video_file(path, brand)
    result = MediaProbe(command_runner=ProbeRunner()).probe(path, filename=filename)
    assert result.category == MediaCategory.VIDEO
    assert result.extension == extension
    assert result.mime_type == mime_type
    assert (result.width, result.height) == (1920, 1080)


def test_video_probe_applies_display_rotation(tmp_path: Path) -> None:
    path = tmp_path / "portrait.mov"
    _video_file(path, b"qt  ")
    result = MediaProbe(command_runner=ProbeRunner(rotation=90)).probe(
        path, filename="portrait.mov"
    )
    assert (result.width, result.height) == (1080, 1920)


def test_heic_probe_uses_registered_pillow_decoder(tmp_path: Path) -> None:
    path = tmp_path / "photo"
    _video_file(path, b"heic")

    class FakeImage:
        format = "HEIF"
        size = (4032, 3024)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def verify(self):
            return None

    with patch("app.common.services.media_probe.register_heif_opener"), patch(
        "app.common.services.media_probe.Image.open", return_value=FakeImage()
    ):
        result = MediaProbe().probe(path, filename="photo")
    assert result.category == MediaCategory.HEIC
    assert result.extension == ".heic"
    assert result.mime_type == "image/heic"
    assert (result.width, result.height) == (4032, 3024)


@pytest.mark.parametrize(
    "payload",
    [
        MediaCommandResult(1, "", "bad container"),
        MediaCommandResult(
            0,
            json.dumps({"streams": [{"codec_type": "audio"}], "format": {}}),
            "",
        ),
    ],
)
def test_corrupt_or_audio_only_video_is_rejected(tmp_path: Path, payload) -> None:
    path = tmp_path / "bad.mp4"
    _video_file(path)
    runner = SimpleNamespace(run=lambda *_args, **_kwargs: payload)
    with pytest.raises(UnsupportedMediaError):
        MediaProbe(command_runner=runner).probe(path, filename=path.name)


def test_command_runner_uses_no_shell_timeout_and_bounded_capture() -> None:
    observed = {}

    def fake_run(arguments, **kwargs):
        observed.update(kwargs)
        kwargs["stdout"].write(b"x" * (BoundedMediaCommandRunner.MAX_CAPTURE_BYTES + 100))
        kwargs["stderr"].write(b"error")
        return SimpleNamespace(returncode=0)

    with patch("app.common.services.media_probe.subprocess.run", fake_run):
        result = BoundedMediaCommandRunner().run(["ffprobe", "input"], timeout_seconds=2)
    assert observed["shell"] is False
    assert observed["timeout"] == 2
    assert len(result.stdout) == BoundedMediaCommandRunner.MAX_CAPTURE_BYTES
    assert result.stderr == "error"


def test_video_thumbnail_is_jpeg_preview_is_null_and_temp_is_cleaned(
    tmp_path: Path,
) -> None:
    storage = LocalStorageService(tmp_path)
    source = storage.original_root / "clip.mp4"
    _video_file(source)
    runner = ThumbnailRunner()
    result = MediaDerivativeService(storage, command_runner=runner).generate(
        original_path=source,
        file_id="a" * 64,
        media=MediaProbeResult(
            MediaCategory.VIDEO,
            ".mp4",
            "video/mp4",
            width=1920,
            height=1080,
            duration_seconds=1.0,
        ),
    )
    assert result.preview_path is None
    assert result.thumb_path is not None and result.thumb_path.suffix == ".jpg"
    with Image.open(result.thumb_path) as thumbnail:
        assert thumbnail.format == "JPEG"
    assert not list(result.thumb_path.parent.glob(".*.jpg"))
    assert "0.000" in runner.calls[0]


def test_video_thumbnail_timeout_is_isolated_and_temp_is_cleaned(tmp_path: Path) -> None:
    storage = LocalStorageService(tmp_path)
    source = storage.original_root / "clip.mp4"
    _video_file(source)
    result = MediaDerivativeService(
        storage, command_runner=ThumbnailRunner(timeout=True)
    ).generate(
        original_path=source,
        file_id="b" * 64,
        media=MediaProbeResult(MediaCategory.VIDEO, ".mp4", "video/mp4"),
    )
    assert result.thumb_path is None
    assert result.failures == ("thumbnail:ffmpeg-timeout",)
    assert not list(storage.thumb_root.rglob(".*.jpg"))


def test_heic_derivatives_are_jpeg_and_original_is_unchanged(
    tmp_path: Path,
) -> None:
    storage = LocalStorageService(tmp_path)
    source = storage.original_root / "photo.heic"
    source.parent.mkdir(parents=True)
    original = io.BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (40, 30), "green").save(original, format="JPEG", exif=exif)
    source.write_bytes(original.getvalue())
    before = source.read_bytes()
    with patch("app.common.services.media_derivatives.register_heif_opener"):
        result = MediaDerivativeService(storage).generate(
            original_path=source,
            file_id="c" * 64,
            media=MediaProbeResult(MediaCategory.HEIC, ".heic", "image/heic"),
        )
    assert source.read_bytes() == before
    assert result.preview_path is not None and result.preview_path.suffix == ".jpg"
    assert result.thumb_path is not None and result.thumb_path.suffix == ".jpg"
    with Image.open(result.preview_path) as preview:
        assert preview.format == "JPEG"
        assert preview.size == (30, 40)


def test_suffixless_video_upload_is_accepted_before_job_creation(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    storage = LocalStorageService(tmp_path)
    probe = SimpleNamespace(
        probe_for_service=lambda *_args, **_kwargs: MediaProbeResult(
            MediaCategory.VIDEO, ".mp4", "video/mp4"
        )
    )
    try:
        with patch.object(upload_router, "storage_service", storage), patch.object(
            upload_router, "media_probe", probe
        ):
            response = upload_router.upload_file(
                file=UploadFile(filename="encoded-name", file=io.BytesIO(b"video")),
                service_name="MemoryKeeper",
                client_file_id=None,
                client_content_sha256=None,
                observation_date=None,
                canonical_target_id=None,
                target_display_name=None,
                db=db,
            )
        assert response["status"] == "WAITING"
        assert db.query(UploadJob).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_unsupported_upload_returns_415_without_job_or_incoming(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    storage = LocalStorageService(tmp_path)
    try:
        with patch.object(upload_router, "storage_service", storage):
            with pytest.raises(HTTPException) as failure:
                upload_router.upload_file(
                    file=UploadFile(filename="payload.bin", file=io.BytesIO(b"not media")),
                    service_name="MemoryKeeper",
                    client_file_id=None,
                    client_content_sha256=None,
                    observation_date=None,
                    canonical_target_id=None,
                    target_display_name=None,
                    db=db,
                )
        assert failure.value.status_code == 415
        assert db.query(UploadJob).count() == 0
        assert not list(storage.incoming_root.glob("*"))
    finally:
        db.close()
        engine.dispose()


def test_video_never_creates_vision_queue() -> None:
    context = PluginContext(
        db=SimpleNamespace(),
        storage_service=SimpleNamespace(),
        common_file=SimpleNamespace(id=1),
        media=MediaProbeResult(MediaCategory.VIDEO, ".mp4", "video/mp4"),
    )
    with patch("worker.background_worker.VisionJobRepository") as repository:
        background_worker._enqueue_vision_job(context.db, context)
    repository.assert_not_called()
    assert "VISION_QUEUE_SKIPPED:VIDEO" in context.processing_log


def test_encoded_video_upload_pipeline_persists_canonical_media_without_vision(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    db = sessions()
    storage = LocalStorageService(tmp_path)
    media = MediaProbeResult(
        MediaCategory.VIDEO,
        ".mp4",
        "video/mp4",
        width=1080,
        height=1920,
        duration_seconds=4.0,
    )

    class FakeProbe:
        def probe_for_service(self, *_args, **_kwargs):
            return media

    class FakeDerivativeService:
        def __init__(self, storage_service):
            self.storage = storage_service

        def generate(self, *, file_id, **_kwargs):
            thumb = self.storage.build_derivative_path(
                kind="thumb", file_id=file_id, extension=".jpg"
            )
            thumb.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (225, 400), "black").save(thumb, format="JPEG")
            return MediaDerivativeResult(None, thumb, 1080, 1920)

    source_bytes = b"\x00\x00\x00\x18ftypisom" + b"video-payload"
    encoded = Header("동영상.mp4", "utf-8").encode()
    try:
        with patch.object(upload_router, "storage_service", storage), patch.object(
            upload_router, "media_probe", FakeProbe()
        ):
            response = upload_router.upload_file(
                file=UploadFile(filename=encoded, file=io.BytesIO(source_bytes)),
                service_name="MemoryKeeper",
                client_file_id=None,
                client_content_sha256=None,
                observation_date=None,
                canonical_target_id=None,
                target_display_name=None,
                db=db,
            )
        with patch.object(background_worker, "StorageService", return_value=storage), patch.object(
            background_worker, "SessionLocal", sessions
        ), patch.object(background_worker, "MediaProbe", return_value=FakeProbe()), patch(
            "worker.plugins.preview_plugin.MediaDerivativeService",
            FakeDerivativeService,
        ):
            assert background_worker.process_next_job(worker_id="video-worker", db=db)

        job = db.query(UploadJob).filter_by(job_id=response["job_id"]).one()
        common_file = db.query(CommonFile).one()
        assert job.status == "COMPLETED"
        assert common_file.original_name == "동영상.mp4"
        assert common_file.extension == ".mp4"
        assert common_file.mime_type == "video/mp4"
        assert (common_file.width, common_file.height) == (1080, 1920)
        assert common_file.preview_path is None
        assert common_file.thumb_path.endswith(".jpg")
        assert storage.resolve_storage_path(common_file.original_path).read_bytes() == source_bytes
        assert db.query(CommonVisionJob).count() == 0
        assert "VISION_QUEUE_SKIPPED:VIDEO" in job.processing_log
    finally:
        db.close()
        engine.dispose()


def test_heic_vision_uses_persisted_jpeg_and_missing_preview_fails_before_client(
    tmp_path: Path,
) -> None:
    storage = LocalStorageService(tmp_path)
    preview = storage.preview_root / "photo.jpg"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"jpeg")
    common_file = SimpleNamespace(
        extension=".heic",
        mime_type="image/heic",
        preview_path=storage.to_relative_path(preview),
        original_path="original/photo.heic",
    )
    context = PluginContext(db=SimpleNamespace(), storage_service=storage, common_file=common_file)
    assert VisionPlugin()._resolve_image_path(context) == preview
    common_file.preview_path = None
    with pytest.raises(ValueError, match="requires a JPEG preview"):
        VisionPlugin()._resolve_image_path(context)


def test_arbitrary_binary_probe_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "anything.jpg"
    path.write_bytes(b"arbitrary binary")
    with pytest.raises(UnsupportedMediaError):
        MediaProbe().probe(path, filename=path.name)
