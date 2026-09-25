"""MemoryKeeper capture-date projection rules and state synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.memorykeeper.models.file_state import MemoryKeeperFileState


class CaptureDateBasis:
    USER = "USER"
    EXIF = "EXIF"
    SOURCE_YEAR = "SOURCE_YEAR"
    IMPORTED = "IMPORTED"
    CREATED = "CREATED"


class CaptureDatePrecision:
    YEAR = "YEAR"
    DATE = "DATE"
    DATETIME = "DATETIME"


@dataclass(frozen=True)
class CaptureDateProjection:
    effective_capture_datetime: datetime | None
    effective_capture_year: int | None
    effective_capture_precision: str | None
    date_basis: str | None


def calculate_capture_date_projection(
    *,
    user_capture_datetime: datetime | None,
    original_capture_datetime: datetime | None,
    imported_at: datetime | None,
    created_at: datetime | None,
    source_capture_year: int | None = None,
    user_capture_precision: str | None = None,
) -> CaptureDateProjection:
    """Resolve one timezone-independent MemoryKeeper capture projection."""
    user_value = _require_naive_wall_clock(
        user_capture_datetime,
        field_name="user_capture_datetime",
    )
    original_value = _require_naive_wall_clock(
        original_capture_datetime,
        field_name="original_capture_datetime",
    )
    if source_capture_year is not None and not 1 <= source_capture_year <= 9999:
        raise ValueError("source_capture_year must be between 1 and 9999")
    if user_value is not None:
        return CaptureDateProjection(
            user_value,
            user_value.year,
            user_capture_precision or CaptureDatePrecision.DATE,
            CaptureDateBasis.USER,
        )
    if source_capture_year is not None:
        if original_value is not None and original_value.year == source_capture_year:
            return CaptureDateProjection(
                original_value,
                original_value.year,
                CaptureDatePrecision.DATETIME,
                CaptureDateBasis.EXIF,
            )
        return CaptureDateProjection(
            None,
            source_capture_year,
            CaptureDatePrecision.YEAR,
            CaptureDateBasis.SOURCE_YEAR,
        )
    if original_value is not None:
        return CaptureDateProjection(
            original_value,
            original_value.year,
            CaptureDatePrecision.DATETIME,
            CaptureDateBasis.EXIF,
        )
    if imported_at is not None:
        imported_value = _instant_to_utc_naive(imported_at)
        return CaptureDateProjection(
            imported_value,
            imported_value.year,
            CaptureDatePrecision.DATETIME,
            CaptureDateBasis.IMPORTED,
        )
    if created_at is not None:
        created_value = _instant_to_utc_naive(created_at)
        return CaptureDateProjection(
            created_value,
            created_value.year,
            CaptureDatePrecision.DATETIME,
            CaptureDateBasis.CREATED,
        )
    return CaptureDateProjection(None, None, None, None)


class MemoryKeeperCaptureDateService:
    """Create or update MemoryKeeper state without owning the transaction."""

    SERVICE_NAME = "MemoryKeeper"

    def __init__(self, db: Session) -> None:
        self.db = db

    def synchronize(
        self,
        *,
        common_file: CommonFile,
        service_link: CommonFileService,
        metadata: CommonFileMetadata | None,
        state: MemoryKeeperFileState | None = None,
        state_missing_known: bool = False,
        initial_favorite: bool = False,
    ) -> MemoryKeeperFileState:
        """Ensure state and apply the current projection without committing."""
        if service_link.file_id != common_file.id:
            raise ValueError("service_link does not belong to common_file")
        if service_link.service_name.casefold() != self.SERVICE_NAME.casefold():
            raise ValueError("MemoryKeeper capture projection requires a MemoryKeeper link")

        if service_link.created_at is None:
            # Server defaults are normally returned by INSERT..RETURNING.  Only
            # refresh when a dialect did not populate the value during flush.
            self.db.refresh(service_link, attribute_names=["created_at"])

        if state is None and not state_missing_known:
            state = self.db.get(MemoryKeeperFileState, common_file.id)
        if state is None:
            state = MemoryKeeperFileState(
                file_id=common_file.id,
                favorite=initial_favorite,
                memo=None,
                revision=0,
            )
            self.db.add(state)

        projection = calculate_capture_date_projection(
            user_capture_datetime=state.user_capture_datetime,
            original_capture_datetime=(
                metadata.original_capture_datetime if metadata is not None else None
            ),
            imported_at=service_link.created_at,
            created_at=common_file.created_at,
            source_capture_year=state.source_capture_year,
            user_capture_precision=state.user_capture_precision,
        )
        state.effective_capture_datetime = projection.effective_capture_datetime
        state.effective_capture_year = projection.effective_capture_year
        state.effective_capture_precision = projection.effective_capture_precision
        state.date_basis = projection.date_basis
        # effective_capture_date and the legacy year are PostgreSQL stored
        # generated columns. Portable unit databases do not implement those
        # expressions, so keep their values coherent there.
        if self.db.get_bind().dialect.name != "postgresql":
            state.effective_capture_date = (
                projection.effective_capture_datetime.date()
                if projection.effective_capture_datetime is not None
                else None
            )
            state.legacy_effective_capture_year = (
                projection.effective_capture_datetime.year
                if projection.effective_capture_datetime is not None
                else None
            )
        # user_capture_precision and revision are intentionally caller-owned.
        self.db.flush()
        return state


def _require_naive_wall_clock(
    value: datetime | None,
    *,
    field_name: str,
) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None and value.utcoffset() is not None:
        raise ValueError(f"{field_name} must be a naive wall-clock datetime")
    return value


def _instant_to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)
