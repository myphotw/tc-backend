"""Pre-generated image and video derivatives for persisted gallery media."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from app.common.services.media_probe import (
    BoundedMediaCommandRunner,
    MediaCategory,
    MediaCommandTimeout,
    MediaProbeResult,
    MediaToolUnavailableError,
    register_heif_opener,
)
from app.common.services.storage_service import StorageService


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class MediaDerivativeResult:
    preview_path: Path | None
    thumb_path: Path | None
    width: int | None
    height: int | None
    failures: tuple[str, ...] = ()


class MediaDerivativeService:
    """Create bounded persisted derivatives without changing the original."""

    VIDEO_THUMB_TIMEOUT_SECONDS = 30.0

    def __init__(
        self,
        storage_service: StorageService,
        *,
        command_runner: BoundedMediaCommandRunner | None = None,
        video_timeout_seconds: float = VIDEO_THUMB_TIMEOUT_SECONDS,
    ) -> None:
        self.storage = storage_service
        self.command_runner = command_runner or BoundedMediaCommandRunner()
        self.video_timeout_seconds = video_timeout_seconds

    def generate(
        self,
        *,
        original_path: str | Path,
        file_id: str,
        media: MediaProbeResult,
        create_preview: bool = True,
        create_thumbnail: bool = True,
    ) -> MediaDerivativeResult:
        if not _SHA256_RE.fullmatch(file_id):
            raise ValueError("file_id must be a lowercase SHA-256 digest")
        source_path = self._validated_source_path(original_path)
        if media.category == MediaCategory.VIDEO:
            return self._generate_video(
                source_path=source_path,
                file_id=file_id,
                media=media,
                create_thumbnail=create_thumbnail,
            )
        if media.category not in {MediaCategory.IMAGE, MediaCategory.HEIC}:
            return MediaDerivativeResult(None, None, media.width, media.height)
        return self._generate_image(
            source_path=source_path,
            file_id=file_id,
            media=media,
            create_preview=create_preview,
            create_thumbnail=create_thumbnail,
        )

    def _generate_image(
        self,
        *,
        source_path: Path,
        file_id: str,
        media: MediaProbeResult,
        create_preview: bool,
        create_thumbnail: bool,
    ) -> MediaDerivativeResult:
        if media.category == MediaCategory.HEIC:
            register_heif_opener()
        output_extension = ".jpg" if media.category == MediaCategory.HEIC else None
        width, height = self.storage.get_image_size(source_path)
        failures: list[str] = []
        preview_path: Path | None = None
        thumb_path: Path | None = None

        if create_preview:
            try:
                preview_path = self.storage.save_preview(
                    source_path,
                    file_id,
                    media.extension,
                    output_extension=output_extension,
                )
                if preview_path is None:
                    failures.append("preview:decoder-returned-no-image")
            except (OSError, UnidentifiedImageError, ValueError) as exc:
                failures.append(f"preview:{type(exc).__name__}")

        if create_thumbnail:
            try:
                thumb_path = self.storage.save_thumbnail(
                    source_path,
                    file_id,
                    media.extension,
                    output_extension=output_extension,
                )
                if thumb_path is None:
                    failures.append("thumbnail:decoder-returned-no-image")
            except (OSError, UnidentifiedImageError, ValueError) as exc:
                failures.append(f"thumbnail:{type(exc).__name__}")

        return MediaDerivativeResult(
            preview_path,
            thumb_path,
            width if width is not None else media.width,
            height if height is not None else media.height,
            tuple(failures),
        )

    def _generate_video(
        self,
        *,
        source_path: Path,
        file_id: str,
        media: MediaProbeResult,
        create_thumbnail: bool,
    ) -> MediaDerivativeResult:
        if not create_thumbnail:
            return MediaDerivativeResult(None, None, media.width, media.height)

        target = self.storage.build_derivative_path(
            kind="thumb",
            file_id=file_id,
            extension=".jpg",
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{file_id}.{uuid4().hex}.jpg"
        seek_seconds = self._poster_seek_seconds(media.duration_seconds)
        try:
            result = self.command_runner.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{seek_seconds:.3f}",
                    "-i",
                    str(source_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=400:400:force_original_aspect_ratio=decrease",
                    "-q:v",
                    "3",
                    str(temporary),
                ],
                timeout_seconds=self.video_timeout_seconds,
            )
            if result.returncode != 0 or not temporary.is_file():
                return MediaDerivativeResult(
                    None,
                    None,
                    media.width,
                    media.height,
                    ("thumbnail:ffmpeg-failed",),
                )
            with Image.open(temporary) as image:
                image.verify()
            os.replace(temporary, target)
            return MediaDerivativeResult(
                None,
                target,
                media.width,
                media.height,
            )
        except MediaCommandTimeout:
            return MediaDerivativeResult(
                None,
                None,
                media.width,
                media.height,
                ("thumbnail:ffmpeg-timeout",),
            )
        except MediaToolUnavailableError:
            return MediaDerivativeResult(
                None,
                None,
                media.width,
                media.height,
                ("thumbnail:ffmpeg-unavailable",),
            )
        except (OSError, UnidentifiedImageError, ValueError):
            return MediaDerivativeResult(
                None,
                None,
                media.width,
                media.height,
                ("thumbnail:invalid-output",),
            )
        finally:
            temporary.unlink(missing_ok=True)

    def _validated_source_path(self, path: str | Path) -> Path:
        resolved = self.storage.resolve_storage_path(path).resolve(strict=False)
        allowed_roots = (
            self.storage.incoming_root.resolve(strict=False),
            self.storage.original_root.resolve(strict=False),
        )
        if not any(self._is_relative_to(resolved, root) for root in allowed_roots):
            raise ValueError("media source path is outside incoming/original storage")
        if not resolved.is_file():
            raise ValueError("media source file does not exist")
        return resolved

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _poster_seek_seconds(duration_seconds: float | None) -> float:
        if duration_seconds is None or duration_seconds <= 1.0:
            return 0.0
        return min(3.0, max(0.5, duration_seconds * 0.1))
