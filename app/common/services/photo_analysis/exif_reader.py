"""EXIF 추출 추상화 계층."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, UnidentifiedImageError


class ExifReader:
    """
    이미지 파일에서 EXIF 메타데이터를 추출한다.

    현재 구현은 Pillow를 사용하며, 추후 piexif / exifread로
    교체할 수 있도록 이 클래스에 추출 책임을 분리한다.
    """

    def read(self, file_path: str | Path) -> dict[str, Any]:
        """
        원본 이미지에서 저장 가능한 EXIF 메타데이터를 추출한다.

        Args:
            file_path: 원본 이미지 경로

        Returns:
            dict[str, Any]: MetadataRepository 필드명과 매핑된 값
        """
        path = Path(file_path)
        metadata: dict[str, Any] = {}

        try:
            with Image.open(path) as image:
                metadata["image_width"] = image.width
                metadata["image_height"] = image.height

                exif = image.getexif()
                if not exif:
                    return self._filter_empty(metadata)

                metadata.update(self._read_basic_fields(exif))
                metadata.update(self._read_gps_fields(exif))
        except (UnidentifiedImageError, OSError):
            return {}

        return self._filter_empty(metadata)

    def _read_basic_fields(self, exif: Image.Exif) -> dict[str, Any]:
        """일반 EXIF 필드를 Metadata 컬럼명으로 변환한다."""
        metadata: dict[str, Any] = {
            "camera_make": self._as_text(exif.get(271)),
            "camera_model": self._as_text(exif.get(272)),
            "lens": self._as_text(exif.get(42036)),
            "datetime_original": self._parse_datetime(
                exif.get(36867) or exif.get(36868) or exif.get(306)
            ),
            "iso": self._as_int(exif.get(34855) or exif.get(34867)),
            "f_number": self._format_rational(exif.get(33437)),
            "exposure_time": self._format_rational(exif.get(33434)),
            "focal_length": self._format_rational(exif.get(37386)),
            "orientation": self._as_int(exif.get(274)),
        }

        width = self._as_int(exif.get(40962))
        height = self._as_int(exif.get(40963))
        if width is not None:
            metadata["image_width"] = width
        if height is not None:
            metadata["image_height"] = height
        return metadata

    def _read_gps_fields(self, exif: Image.Exif) -> dict[str, Any]:
        """GPS 좌표를 decimal degree로 변환한다."""
        gps_info = self._get_gps_info(exif)
        if not gps_info:
            return {}

        gps_lat = self._convert_gps_coordinate(gps_info.get(2), gps_info.get(1))
        gps_lon = self._convert_gps_coordinate(gps_info.get(4), gps_info.get(3))
        gps_alt = self._convert_gps_altitude(gps_info.get(6), gps_info.get(5))

        return {
            "gps_lat": gps_lat,
            "gps_lon": gps_lon,
            "gps_alt": gps_alt,
        }

    def _get_gps_info(self, exif: Image.Exif) -> dict[int, Any]:
        """Pillow 버전에 따라 GPS IFD를 안전하게 읽는다."""
        try:
            if hasattr(ExifTags, "IFD"):
                return dict(exif.get_ifd(ExifTags.IFD.GPSInfo))
        except Exception:
            pass

        raw_gps = exif.get(34853)
        if isinstance(raw_gps, dict):
            return raw_gps
        return {}

    def _convert_gps_coordinate(
        self,
        value: Any,
        ref: str | bytes | None,
    ) -> float | None:
        """EXIF GPS 좌표를 decimal degree로 변환한다."""
        if not value or ref is None:
            return None

        try:
            degrees, minutes, seconds = value
            coordinate = (
                float(degrees)
                + (float(minutes) / 60.0)
                + (float(seconds) / 3600.0)
            )
        except (TypeError, ValueError):
            return None

        ref_value = ref.decode() if isinstance(ref, bytes) else str(ref)
        if ref_value in {"S", "W"}:
            coordinate *= -1
        return coordinate

    def _convert_gps_altitude(
        self,
        value: Any,
        ref: Any,
    ) -> float | None:
        """EXIF GPS 고도를 미터 단위로 변환한다."""
        if value is None:
            return None

        try:
            altitude = float(value)
        except (TypeError, ValueError):
            numerator = getattr(value, "numerator", None)
            denominator = getattr(value, "denominator", None)
            if numerator is None or denominator in {None, 0}:
                return None
            altitude = float(numerator) / float(denominator)

        # 1이면 sea level 아래
        if ref == 1:
            altitude *= -1
        return altitude

    def _parse_datetime(self, value: Any) -> datetime | None:
        """EXIF datetime 문자열을 datetime으로 변환한다."""
        if value is None:
            return None
        text = self._as_text(value)
        if text is None:
            return None

        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    def _format_rational(self, value: Any) -> str | None:
        """EXIF rational 값을 문자열로 변환한다."""
        if value is None:
            return None
        numerator = getattr(value, "numerator", None)
        denominator = getattr(value, "denominator", None)
        if numerator is not None and denominator not in {None, 0}:
            return f"{numerator}/{denominator}"
        return str(value)

    def _as_text(self, value: Any) -> str | None:
        """문자열 값을 정규화한다."""
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode(errors="ignore")
        text = str(value).strip()
        return text or None

    def _as_int(self, value: Any) -> int | None:
        """정수 값을 정규화한다."""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _filter_empty(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """비어 있는 값을 제거한다."""
        return {
            key: value
            for key, value in metadata.items()
            if value is not None and value != ""
        }
