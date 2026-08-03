"""MemoryKeeper와 AstroJournal 공통 파일 저장 서비스."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from app.common.config import settings

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS: set[str] = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}


class StorageService:
    """
    MemoryKeeper와 AstroJournal에서 공통으로 사용하는
    파일 저장 서비스.

    SHA256 해시 기반 디렉터리 구조를 사용하며,
    모든 Storage 경로는 settings에서 가져온다.
    """

    PREVIEW_MAX_SIZE: tuple[int, int] = (2048, 2048)
    THUMB_MAX_SIZE: tuple[int, int] = (400, 400)

    @property
    def storage_root(self) -> Path:
        """
        Photo Platform 루트 경로를 반환한다.

        Returns:
            Path: settings.PHOTO_PLATFORM_ROOT 경로
        """
        return settings.photo_platform_root_path

    @property
    def incoming_root(self) -> Path:
        """
        업로드 직후 원본 파일이 임시로 저장되는 경로를 반환한다.

        Returns:
            Path: settings.INCOMING_DIR 경로
        """
        return settings.incoming_dir_path

    @property
    def original_root(self) -> Path:
        """원본 저장 경로를 반환한다."""
        return settings.original_dir_path

    @property
    def preview_root(self) -> Path:
        """Preview 저장 경로를 반환한다."""
        return settings.preview_dir_path

    @property
    def thumb_root(self) -> Path:
        """Thumbnail 저장 경로를 반환한다."""
        return settings.thumb_dir_path

    @property
    def export_root(self) -> Path:
        """Export 저장 경로를 반환한다."""
        return settings.export_dir_path

    @property
    def cache_root(self) -> Path:
        """Cache 저장 경로를 반환한다."""
        return settings.cache_dir_path

    @property
    def temp_root(self) -> Path:
        """Temp 저장 경로를 반환한다."""
        return settings.temp_dir_path

    def save_incoming(self, file: UploadFile, job_id: str) -> str:
        """
        Upload API에서 받은 파일을 INCOMING_DIR에 저장한다.

        Args:
            file: FastAPI UploadFile
            job_id: 업로드 작업 UUID

        Returns:
            str: PHOTO_PLATFORM_ROOT 기준 상대 경로
        """
        original_name = file.filename or "unknown"
        safe_name = self._sanitize_filename(original_name)
        incoming_path = self.incoming_root / f"{job_id}_{safe_name}"
        incoming_path.parent.mkdir(parents=True, exist_ok=True)

        with incoming_path.open("wb") as output:
            shutil.copyfileobj(file.file, output)

        logger.info("Saved incoming file: %s", incoming_path)
        return self._to_relative_path(incoming_path)

    def calculate_sha256(self, path: Path) -> str:
        """
        파일의 SHA256 해시를 계산한다.

        Args:
            path: 파일 경로

        Returns:
            str: SHA256 해시
        """
        digest = hashlib.sha256()
        with path.open("rb") as input_file:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def resolve_storage_path(self, path: str | Path) -> Path:
        """
        PHOTO_PLATFORM_ROOT 기준 상대 경로 또는 절대 경로를 Path로 변환한다.

        Args:
            path: 상대/절대 경로

        Returns:
            Path: 실제 파일 경로
        """
        value = Path(path)
        if value.is_absolute():
            return value
        return self.storage_root / value

    def move_to_storage(
        self,
        incoming_path: str | Path,
        file_id: str,
        extension: str,
    ) -> Path:
        """
        incoming 파일을 최종 ORIGINAL_DIR로 이동한다.

        Args:
            incoming_path: incoming 파일 경로
            file_id: SHA256 해시
            extension: 확장자

        Returns:
            Path: 이동된 original 파일 경로
        """
        source_path = self.resolve_storage_path(incoming_path)
        target_path = self._build_path(
            self.original_root,
            file_id,
            self._normalize_extension(extension),
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if target_path.exists():
            source_path.unlink(missing_ok=True)
            return target_path

        source_path.replace(target_path)
        logger.info("Moved incoming file to original storage: %s", target_path)
        return target_path

    def save_original(
        self,
        incoming_path: str | Path,
        file_id: str,
        extension: str,
    ) -> Path:
        """
        original 파일을 ORIGINAL_DIR에 저장한다.

        현재 구현은 incoming 파일을 rename(move)하여 원본 바이트를 보존한다.
        """
        return self.move_to_storage(incoming_path, file_id, extension)

    def save_preview(
        self,
        original_path: str | Path,
        file_id: str,
        extension: str,
    ) -> Path | None:
        """
        원본 이미지에서 preview 이미지를 PREVIEW_DIR에 생성한다.

        Args:
            original_path: original 파일 경로
            file_id: SHA256 해시
            extension: 확장자

        Returns:
            Path | None: preview 경로 또는 이미지가 아닌 경우 None
        """
        return self._create_image_variant(
            original_path=original_path,
            target_root=self.preview_root,
            file_id=file_id,
            extension=extension,
            max_size=self.PREVIEW_MAX_SIZE,
        )

    def save_thumbnail(
        self,
        original_path: str | Path,
        file_id: str,
        extension: str,
    ) -> Path | None:
        """
        원본 이미지에서 thumbnail 이미지를 THUMB_DIR에 생성한다.

        Args:
            original_path: original 파일 경로
            file_id: SHA256 해시
            extension: 확장자

        Returns:
            Path | None: thumbnail 경로 또는 이미지가 아닌 경우 None
        """
        return self._create_image_variant(
            original_path=original_path,
            target_root=self.thumb_root,
            file_id=file_id,
            extension=extension,
            max_size=self.THUMB_MAX_SIZE,
        )

    def get_image_size(self, path: str | Path) -> tuple[int | None, int | None]:
        """
        이미지 크기를 조회한다.

        Args:
            path: 이미지 파일 경로

        Returns:
            tuple[int | None, int | None]: width, height
        """
        try:
            with Image.open(self.resolve_storage_path(path)) as image:
                image = ImageOps.exif_transpose(image)
                return image.size
        except (UnidentifiedImageError, OSError):
            return None, None

    def delete_incoming(self, incoming_path: str | Path) -> None:
        """
        incoming 파일을 삭제한다.

        Args:
            incoming_path: incoming 파일 경로
        """
        self.resolve_storage_path(incoming_path).unlink(missing_ok=True)

    def to_relative_path(self, path: Path) -> str:
        """
        PHOTO_PLATFORM_ROOT 기준 상대 경로를 POSIX 문자열로 반환한다.

        Args:
            path: 파일 경로

        Returns:
            str: PHOTO_PLATFORM_ROOT 기준 상대 경로
        """
        return self._to_relative_path(path)

    def save_file(self, file: UploadFile) -> dict[str, Any]:
        """
        업로드 파일을 해시 기반 경로에 저장하고,
        이미지인 경우 preview / thumb 를 생성한다.

        Args:
            file: FastAPI UploadFile

        Returns:
            dict[str, Any]: 저장 결과 메타데이터
        """
        filename = file.filename or "unknown"
        extension = self._normalize_extension(Path(filename).suffix)
        content = file.file.read()
        sha256 = hashlib.sha256(content).hexdigest()
        size = len(content)

        original_path = self._build_path(self.original_root, sha256, extension)
        original_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.write_bytes(content)
        logger.info("Saved original file: %s (size=%s)", original_path, size)

        preview_path: Path | None = None
        thumb_path: Path | None = None
        width: int | None = None
        height: int | None = None

        if self._is_image(extension, content):
            try:
                with Image.open(original_path) as image:
                    image = ImageOps.exif_transpose(image)
                    width, height = image.size
                    preview_path = self._create_resized_image(
                        image=image,
                        target_root=self.preview_root,
                        sha256=sha256,
                        extension=extension,
                        max_size=self.PREVIEW_MAX_SIZE,
                    )
                    thumb_path = self._create_resized_image(
                        image=image,
                        target_root=self.thumb_root,
                        sha256=sha256,
                        extension=extension,
                        max_size=self.THUMB_MAX_SIZE,
                    )
            except Exception:
                logger.exception(
                    "Failed to generate preview/thumb for file_id=%s",
                    sha256,
                )
                preview_path = None
                thumb_path = None

        return {
            "file_id": sha256,
            "original_path": self._to_relative_path(original_path),
            "preview_path": (
                self._to_relative_path(preview_path) if preview_path else None
            ),
            "thumb_path": (
                self._to_relative_path(thumb_path) if thumb_path else None
            ),
            "sha256": sha256,
            "size": size,
            "width": width,
            "height": height,
            "extension": extension,
        }

    def get_original_path(self, file_id: str) -> Path:
        """
        원본 파일의 절대 경로를 반환한다.

        Args:
            file_id: SHA256 해시 (file_id)

        Returns:
            Path: original 파일 절대 경로

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때
        """
        path = self._find_file(self.original_root, file_id)
        if path is None:
            raise FileNotFoundError(f"Original file not found: {file_id}")
        return path.resolve()

    def get_preview_path(self, file_id: str) -> Path:
        """
        Preview 파일의 절대 경로를 반환한다.

        Args:
            file_id: SHA256 해시 (file_id)

        Returns:
            Path: preview 파일 절대 경로

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때
        """
        path = self._find_file(self.preview_root, file_id)
        if path is None:
            raise FileNotFoundError(f"Preview file not found: {file_id}")
        return path.resolve()

    def get_thumb_path(self, file_id: str) -> Path:
        """
        Thumbnail 파일의 절대 경로를 반환한다.

        Args:
            file_id: SHA256 해시 (file_id)

        Returns:
            Path: thumb 파일 절대 경로

        Raises:
            FileNotFoundError: 파일이 존재하지 않을 때
        """
        path = self._find_file(self.thumb_root, file_id)
        if path is None:
            raise FileNotFoundError(f"Thumb file not found: {file_id}")
        return path.resolve()

    def delete_file(self, file_id: str) -> dict[str, bool]:
        """
        original / preview / thumb 파일을 삭제한다.

        Args:
            file_id: SHA256 해시 (file_id)

        Returns:
            dict[str, bool]: 저장소별 삭제 성공 여부
        """
        result = {
            "original": self._delete_root_file(self.original_root, file_id),
            "preview": self._delete_root_file(self.preview_root, file_id),
            "thumb": self._delete_root_file(self.thumb_root, file_id),
        }
        logger.info("Deleted files for file_id=%s: %s", file_id, result)
        return result

    def _build_path(self, root_dir: Path, sha256: str, extension: str) -> Path:
        """
        해시 기반 저장 경로를 생성한다.

        Args:
            root_dir: settings에서 가져온 저장 루트
            sha256: SHA256 해시
            extension: 확장자 (.jpg 등)

        Returns:
            Path: 저장 경로
        """
        return (
            root_dir
            / sha256[:2]
            / sha256[2:4]
            / f"{sha256}{extension}"
        )

    def _find_file(self, root_dir: Path, file_id: str) -> Path | None:
        """
        지정된 저장 루트에서 file_id에 해당하는 파일을 찾는다.

        Args:
            root_dir: settings에서 가져온 저장 루트
            file_id: SHA256 해시

        Returns:
            Path | None: 찾은 파일 경로 또는 None
        """
        directory = root_dir / file_id[:2] / file_id[2:4]
        if not directory.exists():
            return None

        matches = sorted(directory.glob(f"{file_id}.*"))
        if not matches:
            exact = directory / file_id
            if exact.is_file():
                return exact
            return None
        return matches[0]

    def _delete_root_file(self, root_dir: Path, file_id: str) -> bool:
        """
        지정된 저장 루트의 파일을 삭제한다.

        Args:
            root_dir: settings에서 가져온 저장 루트
            file_id: SHA256 해시

        Returns:
            bool: 삭제 성공 여부
        """
        path = self._find_file(root_dir, file_id)
        if path is None:
            return False
        try:
            path.unlink()
            return True
        except OSError:
            logger.exception(
                "Failed to delete file: root=%s file_id=%s path=%s",
                root_dir,
                file_id,
                path,
            )
            return False

    def _create_image_variant(
        self,
        original_path: str | Path,
        target_root: Path,
        file_id: str,
        extension: str,
        max_size: tuple[int, int],
    ) -> Path | None:
        """
        원본 파일에서 preview 또는 thumbnail 이미지를 생성한다.

        Args:
            original_path: original 파일 경로
            target_root: settings에서 가져온 저장 루트
            file_id: SHA256 해시
            extension: 확장자
            max_size: thumbnail 최대 크기 (가로, 세로)

        Returns:
            Path | None: 생성된 이미지 경로 또는 이미지가 아닌 경우 None
        """
        path = self.resolve_storage_path(original_path)
        ext = self._normalize_extension(extension or path.suffix)
        if ext.lower() not in IMAGE_EXTENSIONS:
            return None

        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                return self._create_resized_image(
                    image=image,
                    target_root=target_root,
                    sha256=file_id,
                    extension=ext,
                    max_size=max_size,
                )
        except (UnidentifiedImageError, OSError):
            logger.info("Skip image variant generation for non-image file: %s", path)
            return None

    def _create_resized_image(
        self,
        image: Image.Image,
        target_root: Path,
        sha256: str,
        extension: str,
        max_size: tuple[int, int],
    ) -> Path:
        """
        원본을 수정하지 않고 복사본을 긴 변 기준으로 리사이즈해 저장한다.

        Args:
            image: 원본 Pillow 이미지 (수정하지 않음)
            target_root: settings에서 가져온 저장 루트
            sha256: SHA256 해시
            extension: 확장자
            max_size: thumbnail 최대 크기 (가로, 세로)

        Returns:
            Path: 저장된 이미지 경로
        """
        resized = image.copy()
        resized.thumbnail(max_size, Image.Resampling.LANCZOS)
        output_path = self._build_path(target_root, sha256, extension)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._save_image(resized, output_path, extension)
        logger.info(
            "Saved resized image: %s (size=%sx%s)",
            output_path,
            resized.width,
            resized.height,
        )
        return output_path

    def _save_image(self, image: Image.Image, path: Path, extension: str) -> None:
        """
        이미지를 디스크에 저장한다.

        Args:
            image: 저장할 Pillow 이미지
            path: 저장 경로
            extension: 확장자
        """
        save_image = image
        ext = extension.lower()
        if ext in {".jpg", ".jpeg"} and image.mode in {"RGBA", "P", "LA"}:
            save_image = image.convert("RGB")

        save_image.save(
            path,
            quality=85,
            optimize=True,
        )

    def _is_image(self, extension: str, content: bytes) -> bool:
        """
        확장자와 파일 내용을 기준으로 이미지 여부를 판별한다.

        Args:
            extension: 확장자
            content: 파일 바이트

        Returns:
            bool: 이미지이면 True
        """
        if extension.lower() not in IMAGE_EXTENSIONS:
            return False

        try:
            from io import BytesIO

            with Image.open(BytesIO(content)) as image:
                image.verify()
            return True
        except (UnidentifiedImageError, OSError):
            logger.warning("File extension looks like image but content is invalid")
            return False

    def _normalize_extension(self, extension: str) -> str:
        """
        확장자를 소문자 .ext 형식으로 정규화한다.

        Args:
            extension: 원본 확장자

        Returns:
            str: 정규화된 확장자
        """
        if not extension:
            return ""
        if not extension.startswith("."):
            extension = f".{extension}"
        return extension.lower()

    def _sanitize_filename(self, filename: str) -> str:
        """
        incoming 저장용 파일명을 안전한 형태로 정리한다.

        Args:
            filename: 원본 파일명

        Returns:
            str: 정리된 파일명
        """
        name = Path(filename).name.strip() or "unknown"
        return re.sub(r"[^A-Za-z0-9가-힣._-]+", "_", name)

    def _to_relative_path(self, path: Path) -> str:
        """
        PHOTO_PLATFORM_ROOT 기준 상대 경로를 POSIX 문자열로 반환한다.

        Args:
            path: 절대/상대 파일 경로

        Returns:
            str: PHOTO_PLATFORM_ROOT 기준 상대 경로
        """
        try:
            return path.resolve().relative_to(self.storage_root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()
