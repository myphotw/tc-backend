"""Storage Rule 인터페이스 및 Service Rule 구현."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any


class StorageRule(ABC):
    """서비스별 최종 저장 상대경로 규칙."""

    rule_name: str = "StorageRule"

    @abstractmethod
    def build_path(self, context: Any) -> str:
        """
        Metadata 기반 상대경로를 반환한다.

        예)
            2026/대한민국/서울/남산타워
        """


class MemoryKeeperStorageRule(StorageRule):
    """
    MemoryKeeper 저장 경로 규칙.

    year / country / city / place_name
    값이 없으면 Unknown을 사용하며 예외를 발생시키지 않는다.
    """

    rule_name = "MemoryKeeper"
    UNKNOWN = "Unknown"

    def build_path(self, context: Any) -> str:
        year = self._resolve_year(context)
        country = self._resolve_field(
            context,
            metadata_keys=("country",),
            attr_names=("resolved_country",),
        )
        city = self._resolve_field(
            context,
            metadata_keys=("city",),
            attr_names=("resolved_city",),
        )
        place_name = self._resolve_field(
            context,
            metadata_keys=("place_name", "place"),
            attr_names=("resolved_place",),
        )
        return "/".join([year, country, city, place_name])

    def _resolve_year(self, context: Any) -> str:
        metadata = getattr(context, "metadata", None) or {}
        datetime_original = metadata.get("datetime_original")
        year = self._extract_year(datetime_original)
        if year is not None:
            return year

        for attr_name in ("datetime_original", "created_at"):
            year = self._extract_year(getattr(context, attr_name, None))
            if year is not None:
                return year

        return str(datetime.now(timezone.utc).year)

    def _resolve_field(
        self,
        context: Any,
        *,
        metadata_keys: tuple[str, ...],
        attr_names: tuple[str, ...],
    ) -> str:
        metadata = getattr(context, "metadata", None) or {}
        for key in metadata_keys:
            value = self._normalize_segment(metadata.get(key))
            if value is not None:
                return value
        for attr_name in attr_names:
            value = self._normalize_segment(getattr(context, attr_name, None))
            if value is not None:
                return value
        return self.UNKNOWN

    @staticmethod
    def _extract_year(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return str(value.year)
        text = str(value).strip()
        if len(text) >= 4 and text[:4].isdigit():
            return text[:4]
        return None

    @staticmethod
    def _normalize_segment(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        # 경로 구분자 및 위험 문자를 제거한다.
        for token in ("\\", "/", ":", "*", "?", "\"", "<", ">", "|"):
            text = text.replace(token, "_")
        text = text.strip(" .")
        return text or None


class AstroJournalStorageRule(StorageRule):
    """
    AstroJournal 저장 경로 규칙.

    AstroJournal / year / canonical target
    값이 없으면 Unknown을 사용하며 예외를 발생시키지 않는다.
    """

    rule_name = "AstroJournal"
    UNKNOWN = "Unknown"
    MAX_TARGET_BYTES = 180
    WINDOWS_RESERVED_NAMES = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }

    def build_path(self, context: Any) -> str:
        year = self._resolve_year(context)
        target = self._resolve_target(context)
        return "/".join([self.rule_name, year, target])

    def _resolve_year(self, context: Any) -> str:
        metadata = getattr(context, "metadata", None) or {}
        for key in ("observation_date", "datetime_original"):
            year = MemoryKeeperStorageRule._extract_year(metadata.get(key))
            if year is not None:
                return year

        year = MemoryKeeperStorageRule._extract_year(
            getattr(context, "datetime_original", None)
        )
        if year is not None:
            return year

        job = getattr(context, "job", None)
        for value in (
            getattr(job, "created_at", None),
            getattr(context, "created_at", None),
        ):
            year = MemoryKeeperStorageRule._extract_year(value)
            if year is not None:
                return year

        return str(datetime.now(timezone.utc).year)

    def _resolve_target(self, context: Any) -> str:
        metadata = getattr(context, "metadata", None) or {}
        for key in ("canonical_target_id", "target_display_name"):
            value = MemoryKeeperStorageRule._normalize_segment(metadata.get(key))
            if value is not None:
                return self._normalize_target_length(value)
        return self.UNKNOWN

    def _normalize_target_length(self, value: str) -> str:
        if value.split(".", 1)[0].upper() in self.WINDOWS_RESERVED_NAMES:
            value = f"_{value}"
        encoded = value.encode("utf-8")
        if len(encoded) <= self.MAX_TARGET_BYTES:
            return value
        shortened = encoded[: self.MAX_TARGET_BYTES].decode(
            "utf-8",
            errors="ignore",
        )
        return shortened.rstrip(" .") or self.UNKNOWN
