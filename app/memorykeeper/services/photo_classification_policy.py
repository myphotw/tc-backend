"""Shared final-classification policy for MemoryKeeper photos."""

from __future__ import annotations

from app.common.models.file_metadata import CommonFileMetadata
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.services.place_matcher import PlaceMatchSource


class MemoryKeeperPhotoCategory:
    NORMAL = "NORMAL"
    DAILY = "DAILY"

    VALUES = frozenset((NORMAL, DAILY))


def effective_photo_category(state: MemoryKeeperFileState | None) -> str:
    """Treat pre-migration or missing state as the legacy NORMAL category."""
    if state is None or not state.photo_category:
        return MemoryKeeperPhotoCategory.NORMAL
    return str(state.photo_category)


def is_daily_photo(state: MemoryKeeperFileState | None) -> bool:
    return effective_photo_category(state) == MemoryKeeperPhotoCategory.DAILY


def is_user_place_decision(metadata: CommonFileMetadata | None) -> bool:
    """USER covers both an explicit Place assignment and explicit no-Place."""
    return metadata is not None and metadata.place_match_source == PlaceMatchSource.USER


def blocks_automatic_place_change(
    metadata: CommonFileMetadata | None,
    state: MemoryKeeperFileState | None,
) -> bool:
    """Return whether automatic matching/reclassification must preserve Place state."""
    return is_daily_photo(state) or is_user_place_decision(metadata)
