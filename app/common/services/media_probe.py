"""Path-based media detection for upload validation and worker processing."""

from __future__ import annotations

import json
import mimetypes
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

from PIL import Image, UnidentifiedImageError


class MediaCategory(str, Enum):
    IMAGE = "image"
    HEIC = "heic"
    VIDEO = "video"
    LEGACY = "legacy"


class MediaProbeError(RuntimeError):
    """Base error for media probing."""


class UnsupportedMediaError(MediaProbeError):
    """The file is not a supported media asset."""


class MediaToolUnavailableError(MediaProbeError):
    """A decoder/probe required for a supported format is unavailable."""


class MediaCommandTimeout(MediaProbeError):
    """An external media command exceeded its deadline."""


@dataclass(frozen=True)
class MediaProbeResult:
    category: MediaCategory
    extension: str
    mime_type: str
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    rotation_degrees: int = 0
    format_name: str | None = None


@dataclass(frozen=True)
class MediaCommandResult:
    returncode: int
    stdout: str
    stderr: str


class BoundedMediaCommandRunner:
    """Run media tools without buffering unbounded output in Python memory."""

    MAX_CAPTURE_BYTES = 64 * 1024

    def run(
        self,
        arguments: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> MediaCommandResult:
        try:
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                completed = subprocess.run(
                    list(arguments),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=timeout_seconds,
                    check=False,
                    shell=False,
                )
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout = stdout_file.read(self.MAX_CAPTURE_BYTES).decode(
                    "utf-8", errors="replace"
                )
                stderr = stderr_file.read(self.MAX_CAPTURE_BYTES).decode(
                    "utf-8", errors="replace"
                )
        except FileNotFoundError as exc:
            raise MediaToolUnavailableError(
                f"media tool is unavailable: {arguments[0]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaCommandTimeout(
                f"media command timed out: {arguments[0]}"
            ) from exc

        return MediaCommandResult(completed.returncode, stdout, stderr)


_IMAGE_FORMATS: dict[str, tuple[str, str]] = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "GIF": (".gif", "image/gif"),
    "WEBP": (".webp", "image/webp"),
    "BMP": (".bmp", "image/bmp"),
    "TIFF": (".tif", "image/tiff"),
}
_FORMAT_SUFFIXES: dict[str, set[str]] = {
    "JPEG": {".jpg", ".jpeg"},
    "TIFF": {".tif", ".tiff"},
}
_HEIF_SUFFIXES = {".heic", ".heif"}
_HEIF_BRANDS = {
    b"heic",
    b"heix",
    b"hevc",
    b"hevx",
    b"heim",
    b"heis",
    b"mif1",
    b"msf1",
}
_QUICKTIME_BRANDS = {b"qt  "}
_MP4_BRANDS = {
    b"3gp5",
    b"isom",
    b"iso2",
    b"iso5",
    b"iso6",
    b"avc1",
    b"dash",
    b"mp41",
    b"mp42",
    b"M4V ",
    b"MSNV",
}


def register_heif_opener() -> None:
    """Register pillow-heif lazily so ordinary image users stay lightweight."""
    try:
        from pillow_heif import register_heif_opener as register
    except ImportError as exc:
        raise MediaToolUnavailableError("pillow-heif is required for HEIC/HEIF") from exc
    register()


class MediaProbe:
    """Identify supported media from file bytes rather than client headers."""

    def __init__(
        self,
        *,
        command_runner: BoundedMediaCommandRunner | None = None,
        ffprobe_timeout_seconds: float = 15.0,
    ) -> None:
        self.command_runner = command_runner or BoundedMediaCommandRunner()
        self.ffprobe_timeout_seconds = ffprobe_timeout_seconds

    def probe_for_service(
        self,
        path: str | Path,
        *,
        filename: str,
        service_name: str,
    ) -> MediaProbeResult:
        """Apply strict MemoryKeeper detection while preserving Astro legacy formats."""
        try:
            return self.probe(path, filename=filename)
        except UnsupportedMediaError:
            if service_name.casefold() != "astrojournal":
                raise
            extension = Path(filename).suffix.lower()
            if not extension:
                raise
            mime_type, _ = mimetypes.guess_type(f"file{extension}")
            return MediaProbeResult(
                category=MediaCategory.LEGACY,
                extension=extension,
                mime_type=mime_type or "application/octet-stream",
            )

    def probe(self, path: str | Path, *, filename: str) -> MediaProbeResult:
        media_path = Path(path)
        if not media_path.is_file():
            raise UnsupportedMediaError("media file does not exist")

        suffix = Path(filename).suffix.lower()
        brand = self._iso_base_media_brand(media_path)
        if suffix in _HEIF_SUFFIXES or brand in _HEIF_BRANDS:
            return self._probe_heif(media_path, suffix=suffix)

        image = self._probe_image(media_path, suffix=suffix)
        if image is not None:
            return image

        if brand in _MP4_BRANDS or brand in _QUICKTIME_BRANDS:
            return self._probe_video(media_path, brand=brand)
        raise UnsupportedMediaError("unsupported media format")

    @staticmethod
    def _iso_base_media_brand(path: Path) -> bytes | None:
        try:
            with path.open("rb") as source:
                header = source.read(32)
        except OSError as exc:
            raise UnsupportedMediaError("unable to read media file") from exc
        if len(header) < 12 or header[4:8] != b"ftyp":
            return None
        return header[8:12]

    @staticmethod
    def _probe_image(path: Path, *, suffix: str) -> MediaProbeResult | None:
        try:
            with Image.open(path) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError):
            return None

        details = _IMAGE_FORMATS.get(image_format)
        if details is None:
            return None
        default_extension, mime_type = details
        allowed_suffixes = _FORMAT_SUFFIXES.get(image_format, {default_extension})
        extension = suffix if suffix in allowed_suffixes else default_extension
        return MediaProbeResult(
            category=MediaCategory.IMAGE,
            extension=extension,
            mime_type=mime_type,
            width=width,
            height=height,
            format_name=image_format,
        )

    @staticmethod
    def _probe_heif(path: Path, *, suffix: str) -> MediaProbeResult:
        register_heif_opener()
        try:
            with Image.open(path) as image:
                image_format = str(image.format or "").upper()
                width, height = image.size
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise UnsupportedMediaError("invalid HEIC/HEIF image") from exc
        if image_format not in {"HEIC", "HEIF"}:
            raise UnsupportedMediaError("invalid HEIC/HEIF image")
        extension = suffix if suffix in _HEIF_SUFFIXES else ".heic"
        return MediaProbeResult(
            category=MediaCategory.HEIC,
            extension=extension,
            mime_type="image/heif" if extension == ".heif" else "image/heic",
            width=width,
            height=height,
            format_name=image_format,
        )

    def _probe_video(self, path: Path, *, brand: bytes) -> MediaProbeResult:
        result = self.command_runner.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_entries",
                "format=format_name,duration:stream=codec_type,width,height:stream_tags=rotate:stream_side_data=rotation",
                str(path),
            ],
            timeout_seconds=self.ffprobe_timeout_seconds,
        )
        if result.returncode != 0:
            raise UnsupportedMediaError("invalid or unsupported video container")
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise UnsupportedMediaError("ffprobe returned invalid metadata") from exc

        streams = payload.get("streams") if isinstance(payload, dict) else None
        if not isinstance(streams, list):
            raise UnsupportedMediaError("video metadata has no streams")
        video_stream = next(
            (
                item
                for item in streams
                if isinstance(item, dict) and item.get("codec_type") == "video"
            ),
            None,
        )
        if video_stream is None:
            raise UnsupportedMediaError("container has no video stream")

        width = self._positive_int(video_stream.get("width"))
        height = self._positive_int(video_stream.get("height"))
        rotation = self._rotation(video_stream)
        if rotation in {90, 270}:
            width, height = height, width

        format_info = payload.get("format") if isinstance(payload, dict) else None
        format_name = (
            str(format_info.get("format_name"))
            if isinstance(format_info, dict) and format_info.get("format_name")
            else None
        )
        duration = self._positive_float(
            format_info.get("duration") if isinstance(format_info, dict) else None
        )
        is_quicktime = brand in _QUICKTIME_BRANDS
        return MediaProbeResult(
            category=MediaCategory.VIDEO,
            extension=".mov" if is_quicktime else ".mp4",
            mime_type="video/quicktime" if is_quicktime else "video/mp4",
            width=width,
            height=height,
            duration_seconds=duration,
            rotation_degrees=rotation,
            format_name=format_name,
        )

    @staticmethod
    def _positive_int(value: object) -> int | None:
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _positive_float(value: object) -> float | None:
        try:
            parsed = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _rotation(stream: dict[str, object]) -> int:
        candidates: list[object] = []
        tags = stream.get("tags")
        if isinstance(tags, dict):
            candidates.append(tags.get("rotate"))
        side_data = stream.get("side_data_list")
        if isinstance(side_data, list):
            candidates.extend(
                item.get("rotation")
                for item in side_data
                if isinstance(item, dict)
            )
        for value in candidates:
            try:
                return int(round(float(value))) % 360  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
        return 0
