from __future__ import annotations

from enum import IntEnum


class MetadataPriority(IntEnum):
    """메타데이터 source 우선순위."""

    USER = 100
    PLATESOLVE = 90
    GPS = 80
    VISION = 70
    EXIF = 60
    SYSTEM = 50

    @classmethod
    def from_source(cls, source: str) -> "MetadataPriority":
        """source 문자열에 해당하는 priority를 반환한다."""
        try:
            return cls[source]
        except KeyError:
            return cls.SYSTEM
