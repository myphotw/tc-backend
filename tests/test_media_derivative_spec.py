from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image, features

from app.common.services.media_derivatives import MediaDerivativeService
from app.common.services.media_probe import MediaCategory, MediaProbeResult
from app.common.services.storage_service import StorageService


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


def _jpeg_source(storage: LocalStorageService, size: tuple[int, int]) -> Path:
    source = storage.original_root / "source.jpg"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "navy").save(source, format="JPEG")
    return source


def _image_media(extension: str = ".jpg", mime_type: str = "image/jpeg"):
    return MediaProbeResult(MediaCategory.IMAGE, extension, mime_type)


@pytest.mark.parametrize(
    ("source_size", "expected_size"),
    [
        ((960, 480), (480, 240)),
        ((480, 960), (240, 480)),
        ((320, 200), (320, 200)),
    ],
)
def test_thumbnail_long_edge_is_480_without_upscale(
    tmp_path: Path,
    source_size: tuple[int, int],
    expected_size: tuple[int, int],
) -> None:
    storage = LocalStorageService(tmp_path)
    source = _jpeg_source(storage, source_size)

    result = MediaDerivativeService(storage).generate(
        original_path=source,
        file_id="a" * 64,
        media=_image_media(),
        create_preview=False,
    )

    assert result.preview_path is None
    assert result.thumb_path is not None
    with Image.open(result.thumb_path) as thumbnail:
        assert thumbnail.size == expected_size


@pytest.mark.parametrize(
    ("source_size", "expected_size"),
    [
        ((3000, 1500), (2560, 1280)),
        ((1500, 3000), (1280, 2560)),
        ((1200, 800), (1200, 800)),
    ],
)
def test_preview_long_edge_is_2560_without_upscale(
    tmp_path: Path,
    source_size: tuple[int, int],
    expected_size: tuple[int, int],
) -> None:
    storage = LocalStorageService(tmp_path)
    source = _jpeg_source(storage, source_size)

    result = MediaDerivativeService(storage).generate(
        original_path=source,
        file_id="b" * 64,
        media=_image_media(),
        create_thumbnail=False,
    )

    assert result.thumb_path is None
    assert result.preview_path is not None
    with Image.open(result.preview_path) as preview:
        assert preview.size == expected_size


def test_exif_orientation_is_applied_before_thumbnail_resize(tmp_path: Path) -> None:
    storage = LocalStorageService(tmp_path)
    source = storage.original_root / "oriented.jpg"
    source.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (600, 300), "green").save(source, format="JPEG", exif=exif)

    result = MediaDerivativeService(storage).generate(
        original_path=source,
        file_id="c" * 64,
        media=_image_media(),
        create_preview=False,
    )

    assert result.thumb_path is not None
    with Image.open(result.thumb_path) as thumbnail:
        assert thumbnail.size == (240, 480)


def test_jpeg_derivatives_keep_quality_85_and_atomic_temp_cleanup(
    tmp_path: Path,
) -> None:
    storage = LocalStorageService(tmp_path)
    source = _jpeg_source(storage, (960, 480))
    observed: list[dict[str, object]] = []
    original_save = Image.Image.save

    def tracking_save(image, fp, format=None, **params):
        observed.append(dict(params))
        return original_save(image, fp, format=format, **params)

    with patch.object(Image.Image, "save", tracking_save):
        result = MediaDerivativeService(storage).generate(
            original_path=source,
            file_id="d" * 64,
            media=_image_media(),
            create_preview=False,
        )

    assert result.thumb_path is not None
    assert observed == [{"quality": 85, "optimize": True}]
    assert not list(result.thumb_path.parent.glob(".*.jpg"))


def test_failed_image_write_preserves_existing_derivative(tmp_path: Path) -> None:
    storage = LocalStorageService(tmp_path)
    target = storage.thumb_root / "aa" / "bb" / "existing.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"existing-derivative")

    with patch.object(Image.Image, "save", side_effect=OSError("synthetic failure")):
        with pytest.raises(OSError, match="synthetic failure"):
            storage._save_image(Image.new("RGB", (10, 10)), target, ".jpg")

    assert target.read_bytes() == b"existing-derivative"
    assert not list(target.parent.glob(".*.jpg"))


@pytest.mark.parametrize(
    ("extension", "mime_type", "image_format"),
    [
        (".png", "image/png", "PNG"),
        pytest.param(
            ".webp",
            "image/webp",
            "WEBP",
            marks=pytest.mark.skipif(
                not features.check("webp"),
                reason="Pillow WebP support is unavailable",
            ),
        ),
    ],
)
def test_png_and_webp_derivatives_keep_their_format(
    tmp_path: Path,
    extension: str,
    mime_type: str,
    image_format: str,
) -> None:
    storage = LocalStorageService(tmp_path)
    source = storage.original_root / f"source{extension}"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (960, 480), "purple").save(source, format=image_format)

    result = MediaDerivativeService(storage).generate(
        original_path=source,
        file_id="e" * 64,
        media=_image_media(extension, mime_type),
        create_preview=False,
    )

    assert result.thumb_path is not None
    assert result.thumb_path.suffix == extension
    with Image.open(result.thumb_path) as thumbnail:
        assert thumbnail.format == image_format
        assert thumbnail.size == (480, 240)
