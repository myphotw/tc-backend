"""Verify and optionally apply a MemoryKeeper source-year reconciliation plan."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import re
import sys
from typing import Iterable, Sequence, TextIO

from sqlalchemy import text
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.common.models.file import CommonFile  # noqa: E402
from app.common.models.file_metadata import CommonFileMetadata  # noqa: E402
from app.common.models.file_service import CommonFileService  # noqa: E402
from app.common.repositories.change_event_repository import (  # noqa: E402
    ChangeEventRepository,
    ChangeOperation,
)
from app.common.repositories.history_repository import HistoryRepository  # noqa: E402
from app.common.repositories.metadata_priority import MetadataPriority  # noqa: E402
from app.memorykeeper.models.file_state import MemoryKeeperFileState  # noqa: E402
from app.memorykeeper.services.capture_date_service import (  # noqa: E402
    CaptureDateBasis,
    CaptureDatePrecision,
    CaptureDateProjection,
    MemoryKeeperCaptureDateService,
    calculate_capture_date_projection,
)
from scripts.plan_memorykeeper_source_year_reconciliation import (  # noqa: E402
    CREATED_FALLBACK,
    EXCLUDE,
    EXIF_SOURCE_YEAR_MATCH,
    EXIF_YEAR_CONFLICT,
    IMPORTED_FALLBACK,
    KEEP_EXACT_EXIF,
    KEEP_EXACT_USER,
    PLAN_CSV_FIELDS,
    SET_YEAR_ONLY,
    USER_EXACT,
    USER_SOURCE_YEAR_CONFLICT,
)


SERVICE_NAME = "MemoryKeeper"
SOURCE_BASIS = "ORIGINAL_PATH"
RESOURCE_TYPE = "MemoryKeeperCaptureDate"
HISTORY_SOURCE = "SOURCE_YEAR_RECONCILIATION"
DEFAULT_BATCH_SIZE = 50
MAX_BATCH_SIZE = 50
VERIFY_BATCH_SIZE = 500

READY_PROVENANCE_ONLY_EXIF = "READY_PROVENANCE_ONLY_EXIF"
READY_YEAR_ONLY_EXIF_CONFLICT = "READY_YEAR_ONLY_EXIF_CONFLICT"
READY_YEAR_ONLY_IMPORTED = "READY_YEAR_ONLY_IMPORTED"
READY_YEAR_ONLY_CREATED = "READY_YEAR_ONLY_CREATED"
KEEP_USER = "KEEP_EXACT_USER"
USER_CONFLICT_REVIEW = "USER_CONFLICT_REVIEW"
ALREADY_APPLIED = "ALREADY_APPLIED"
STALE_REVISION = "STALE_REVISION"
MISSING_FILE = "MISSING_FILE"
MISSING_MEMORYKEEPER_LINK = "MISSING_MEMORYKEEPER_LINK"
MISSING_STATE = "MISSING_STATE"
SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
PROJECTION_CONFLICT = "PROJECTION_CONFLICT"
OTHER_CONFLICT = "OTHER_CONFLICT"
PLAN_EXCLUDED = "PLAN_EXCLUDED"

READY_STATUSES = frozenset(
    {
        READY_PROVENANCE_ONLY_EXIF,
        READY_YEAR_ONLY_EXIF_CONFLICT,
        READY_YEAR_ONLY_IMPORTED,
        READY_YEAR_ONLY_CREATED,
    }
)
YEAR_ONLY_READY_STATUSES = frozenset(
    {
        READY_YEAR_ONLY_EXIF_CONFLICT,
        READY_YEAR_ONLY_IMPORTED,
        READY_YEAR_ONLY_CREATED,
    }
)
BLOCKING_STATUSES = frozenset(
    {
        STALE_REVISION,
        MISSING_FILE,
        MISSING_MEMORYKEEPER_LINK,
        MISSING_STATE,
        SNAPSHOT_MISMATCH,
        PROJECTION_CONFLICT,
        OTHER_CONFLICT,
    }
)

REPORT_CSV_FIELDS = (
    "sha256",
    "file_id",
    "plan_action",
    "plan_reason",
    "verification_status",
    "source_year",
    "current_date_basis",
    "current_effective_year",
    "current_effective_date",
    "current_effective_datetime",
    "current_precision",
    "current_revision",
    "message",
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_ACTION_REASON = {
    KEEP_EXACT_EXIF: frozenset({EXIF_SOURCE_YEAR_MATCH}),
    KEEP_EXACT_USER: frozenset({USER_EXACT}),
    SET_YEAR_ONLY: frozenset(
        {EXIF_YEAR_CONFLICT, IMPORTED_FALLBACK, CREATED_FALLBACK}
    ),
}


class ReconciliationValidationError(ValueError):
    """Raised when a plan or live snapshot cannot be trusted."""


class ReconciliationApplyError(RuntimeError):
    """Raised when a locked batch no longer matches its verified snapshot."""


@dataclass(frozen=True)
class ReconciliationPlanRow:
    row_number: int
    sha256: str | None
    source_year: int | None
    action: str
    reason: str
    review_reason: str | None
    current_date_basis: str | None
    current_effective_year: int | None
    current_effective_date: date | None
    current_effective_datetime: datetime | None
    current_user_datetime: datetime | None
    current_user_precision: str | None
    current_revision: int | None
    source_path_count: int


@dataclass(frozen=True)
class LoadedContext:
    common_file: CommonFile | None
    service_link: CommonFileService | None
    metadata: CommonFileMetadata | None
    state: MemoryKeeperFileState | None


@dataclass(frozen=True)
class VerificationResult:
    plan: ReconciliationPlanRow
    file_id: int | None
    verification_status: str
    current_date_basis: str | None
    current_effective_year: int | None
    current_effective_date: date | None
    current_effective_datetime: datetime | None
    current_precision: str | None
    current_revision: int | None
    message: str = ""

    def csv_dict(self) -> dict[str, object]:
        return {
            "sha256": self.plan.sha256 or "",
            "file_id": self.file_id if self.file_id is not None else "",
            "plan_action": self.plan.action,
            "plan_reason": self.plan.reason,
            "verification_status": self.verification_status,
            "source_year": (
                self.plan.source_year if self.plan.source_year is not None else ""
            ),
            "current_date_basis": self.current_date_basis or "",
            "current_effective_year": (
                self.current_effective_year
                if self.current_effective_year is not None
                else ""
            ),
            "current_effective_date": (
                self.current_effective_date.isoformat()
                if self.current_effective_date is not None
                else ""
            ),
            "current_effective_datetime": (
                self.current_effective_datetime.isoformat()
                if self.current_effective_datetime is not None
                else ""
            ),
            "current_precision": self.current_precision or "",
            "current_revision": (
                self.current_revision if self.current_revision is not None else ""
            ),
            "message": self.message,
        }


@dataclass(frozen=True)
class ApplyStats:
    applied: int = 0
    already_applied_during_lock: int = 0
    committed_batches: int = 0


def load_plan_csv(path: Path) -> list[ReconciliationPlanRow]:
    """Load the planner contract and reject malformed or duplicate actions."""
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        missing = set(PLAN_CSV_FIELDS) - set(reader.fieldnames or ())
        if missing:
            raise ReconciliationValidationError(
                "plan CSV is missing required columns: " + ", ".join(sorted(missing))
            )
        rows = [
            _parse_plan_row(values, row_number=row_number)
            for row_number, values in enumerate(reader, start=2)
        ]

    seen: dict[str, int] = {}
    for row in rows:
        if row.sha256 is None:
            continue
        previous = seen.setdefault(row.sha256, row.row_number)
        if previous != row.row_number:
            raise ReconciliationValidationError(
                f"duplicate sha256 at rows {previous} and {row.row_number}"
            )
    return sorted(rows, key=_plan_sort_key)


class MemoryKeeperSourceYearReconciler:
    """Verify plans and apply bounded, idempotent source-year batches."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.projection = MemoryKeeperCaptureDateService(db)
        self.history = HistoryRepository(db)
        self.changes = ChangeEventRepository(db)

    def verify_all(
        self,
        plans: Sequence[ReconciliationPlanRow],
    ) -> list[VerificationResult]:
        results: list[VerificationResult] = []
        actionable = [row for row in plans if row.action != EXCLUDE]
        excluded = [row for row in plans if row.action == EXCLUDE]
        for row in excluded:
            results.append(
                self._result(
                    row,
                    None,
                    PLAN_EXCLUDED,
                    f"excluded by plan reason {row.reason}",
                )
            )
        for batch in _chunks(actionable, VERIFY_BATCH_SIZE):
            contexts = self._load_contexts(batch, lock=False)
            results.extend(
                self._verify_context(row, contexts.get(row.sha256 or ""))
                for row in batch
            )
        return sorted(results, key=lambda result: _plan_sort_key(result.plan))

    def apply_verified(
        self,
        results: Sequence[VerificationResult],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> ApplyStats:
        _validate_batch_size(batch_size)
        blocking = [
            result for result in results if result.verification_status in BLOCKING_STATUSES
        ]
        if blocking:
            raise ReconciliationApplyError(
                f"apply refused: {len(blocking)} blocking verification conflicts"
            )

        ready = sorted(
            (result for result in results if result.verification_status in READY_STATUSES),
            key=lambda result: (
                result.file_id if result.file_id is not None else 0,
                result.plan.sha256 or "",
            ),
        )
        stats = ApplyStats()
        for batch in _chunks(ready, batch_size):
            plans = [result.plan for result in batch]
            expected_status = {
                result.plan.sha256: result.verification_status for result in batch
            }
            try:
                contexts = self._load_contexts(plans, lock=True)
                applied = 0
                already = 0
                for plan in plans:
                    current = self._verify_context(
                        plan,
                        contexts.get(plan.sha256 or ""),
                    )
                    if current.verification_status == ALREADY_APPLIED:
                        already += 1
                        continue
                    if current.verification_status != expected_status[plan.sha256]:
                        raise ReconciliationApplyError(
                            f"{plan.sha256}: locked verification changed to "
                            f"{current.verification_status}"
                        )
                    context = contexts[plan.sha256 or ""]
                    self._apply_context(plan, context)
                    applied += 1
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            stats = ApplyStats(
                applied=stats.applied + applied,
                already_applied_during_lock=(
                    stats.already_applied_during_lock + already
                ),
                committed_batches=stats.committed_batches + 1,
            )
        return stats

    def _load_contexts(
        self,
        plans: Sequence[ReconciliationPlanRow],
        *,
        lock: bool,
    ) -> dict[str, LoadedContext]:
        hashes = sorted({row.sha256 for row in plans if row.sha256 is not None})
        if not hashes:
            return {}
        files_query = self.db.query(CommonFile).filter(CommonFile.file_id.in_(hashes))
        if lock:
            files_query = files_query.order_by(CommonFile.id.asc()).with_for_update(
                of=CommonFile
            )
        files = files_query.all()
        file_ids = [item.id for item in files]

        links_query = self.db.query(CommonFileService).filter(
            CommonFileService.file_id.in_(file_ids),
            CommonFileService.service_name == SERVICE_NAME,
        )
        states_query = self.db.query(MemoryKeeperFileState).filter(
            MemoryKeeperFileState.file_id.in_(file_ids)
        )
        metadata_query = self.db.query(CommonFileMetadata).filter(
            CommonFileMetadata.file_id.in_(file_ids)
        )
        if lock:
            links_query = links_query.order_by(
                CommonFileService.file_id.asc()
            ).with_for_update()
            states_query = states_query.order_by(
                MemoryKeeperFileState.file_id.asc()
            ).with_for_update()
            metadata_query = metadata_query.order_by(
                CommonFileMetadata.file_id.asc()
            ).with_for_update()
        links = {item.file_id: item for item in links_query.all()}
        states = {item.file_id: item for item in states_query.all()}
        metadata = {item.file_id: item for item in metadata_query.all()}
        contexts = {
            item.file_id: LoadedContext(
                common_file=item,
                service_link=links.get(item.id),
                metadata=metadata.get(item.id),
                state=states.get(item.id),
            )
            for item in files
        }
        for sha256 in hashes:
            contexts.setdefault(
                sha256,
                LoadedContext(None, None, None, None),
            )
        return contexts

    def _verify_context(
        self,
        plan: ReconciliationPlanRow,
        context: LoadedContext | None,
    ) -> VerificationResult:
        if context is None or context.common_file is None:
            return self._result(plan, context, MISSING_FILE, "file does not exist")
        common_file = context.common_file
        if common_file.deleted:
            return self._result(plan, context, MISSING_FILE, "file is deleted")
        if context.service_link is None:
            return self._result(
                plan,
                context,
                MISSING_MEMORYKEEPER_LINK,
                "MemoryKeeper service link is missing",
            )
        state = context.state
        if state is None:
            return self._result(plan, context, MISSING_STATE, "file state is missing")

        if state.user_capture_datetime is not None or state.date_basis == CaptureDateBasis.USER:
            user_year = (
                state.user_capture_datetime.year
                if state.user_capture_datetime is not None
                else None
            )
            status = (
                USER_CONFLICT_REVIEW
                if user_year is not None and user_year != plan.source_year
                else KEEP_USER
            )
            return self._result(
                plan,
                context,
                status,
                "current USER capture date overrides the plan",
            )

        target = self._target_projection(plan, context)
        source_state = (state.source_capture_year, state.source_capture_year_basis)
        expected_source = (plan.source_year, SOURCE_BASIS)
        if source_state == expected_source:
            if target is not None and self._state_matches_target(state, target):
                return self._result(
                    plan,
                    context,
                    ALREADY_APPLIED,
                    "source provenance and projection are already applied",
                )
            return self._result(
                plan,
                context,
                PROJECTION_CONFLICT,
                "source provenance exists but projection is inconsistent",
            )
        if source_state != (None, None):
            return self._result(
                plan,
                context,
                OTHER_CONFLICT,
                "existing source provenance differs from the plan",
            )
        if plan.current_revision != int(state.revision or 0):
            return self._result(
                plan,
                context,
                STALE_REVISION,
                "current revision differs from the plan snapshot",
            )
        if not self._snapshot_matches(plan, state):
            return self._result(
                plan,
                context,
                SNAPSHOT_MISMATCH,
                "current capture-date snapshot differs from the plan",
            )
        if plan.action == KEEP_EXACT_USER:
            status = (
                USER_CONFLICT_REVIEW
                if plan.review_reason == USER_SOURCE_YEAR_CONFLICT
                else KEEP_USER
            )
            return self._result(plan, context, status, "USER date is never automatic")
        if target is None or not self._projection_matches_plan(plan, target):
            return self._result(
                plan,
                context,
                PROJECTION_CONFLICT,
                "central projection does not produce the planned result",
            )
        return self._result(
            plan,
            context,
            _ready_status(plan),
            "live snapshot verified",
        )

    def _target_projection(
        self,
        plan: ReconciliationPlanRow,
        context: LoadedContext,
    ) -> CaptureDateProjection | None:
        if (
            plan.source_year is None
            or context.common_file is None
            or context.service_link is None
            or context.state is None
        ):
            return None
        metadata = context.metadata
        state = context.state
        return calculate_capture_date_projection(
            user_capture_datetime=state.user_capture_datetime,
            user_capture_precision=state.user_capture_precision,
            original_capture_datetime=(
                metadata.original_capture_datetime if metadata is not None else None
            ),
            imported_at=context.service_link.created_at,
            created_at=context.common_file.created_at,
            source_capture_year=plan.source_year,
        )

    @staticmethod
    def _snapshot_matches(
        plan: ReconciliationPlanRow,
        state: MemoryKeeperFileState,
    ) -> bool:
        return (
            state.date_basis == plan.current_date_basis
            and state.effective_capture_year == plan.current_effective_year
            and state.effective_capture_date == plan.current_effective_date
            and state.effective_capture_datetime == plan.current_effective_datetime
            and state.user_capture_datetime == plan.current_user_datetime
            and state.user_capture_precision == plan.current_user_precision
        )

    @staticmethod
    def _projection_matches_plan(
        plan: ReconciliationPlanRow,
        target: CaptureDateProjection,
    ) -> bool:
        if plan.action == KEEP_EXACT_EXIF:
            return (
                target.date_basis == CaptureDateBasis.EXIF
                and target.effective_capture_precision == CaptureDatePrecision.DATETIME
                and target.effective_capture_datetime
                == plan.current_effective_datetime
                and target.effective_capture_year == plan.source_year
            )
        if plan.action == SET_YEAR_ONLY:
            return (
                target.date_basis == CaptureDateBasis.SOURCE_YEAR
                and target.effective_capture_precision == CaptureDatePrecision.YEAR
                and target.effective_capture_datetime is None
                and target.effective_capture_year == plan.source_year
            )
        return False

    @staticmethod
    def _state_matches_target(
        state: MemoryKeeperFileState,
        target: CaptureDateProjection,
    ) -> bool:
        expected_date = (
            target.effective_capture_datetime.date()
            if target.effective_capture_datetime is not None
            else None
        )
        return (
            state.effective_capture_datetime == target.effective_capture_datetime
            and state.effective_capture_date == expected_date
            and state.effective_capture_year == target.effective_capture_year
            and state.effective_capture_precision
            == target.effective_capture_precision
            and state.date_basis == target.date_basis
        )

    def _apply_context(
        self,
        plan: ReconciliationPlanRow,
        context: LoadedContext,
    ) -> None:
        common_file = context.common_file
        service_link = context.service_link
        state = context.state
        if common_file is None or service_link is None or state is None:
            raise ReconciliationApplyError(f"{plan.sha256}: incomplete locked context")

        user_before = (state.user_capture_datetime, state.user_capture_precision)
        original_before = (
            context.metadata.original_capture_datetime
            if context.metadata is not None
            else None
        )
        old_year = state.source_capture_year
        old_basis = state.source_capture_year_basis
        state.source_capture_year = plan.source_year
        state.source_capture_year_basis = SOURCE_BASIS
        target = self._target_projection(plan, context)
        if target is None or not self._projection_matches_plan(plan, target):
            raise ReconciliationApplyError(
                f"{plan.sha256}: projection changed while applying"
            )
        self.projection.synchronize(
            common_file=common_file,
            service_link=service_link,
            metadata=context.metadata,
            state=state,
        )
        state.revision = int(state.revision or 0) + 1
        state.updated_at = datetime.now(timezone.utc)
        self.history.create_histories(
            items=[
                {
                    "file_id": common_file.id,
                    "field_name": "memorykeeper_source_capture_year",
                    "old_value": old_year,
                    "new_value": plan.source_year,
                    "source": HISTORY_SOURCE,
                    "priority": MetadataPriority.SYSTEM,
                    "modified_by": self.__class__.__name__,
                    "approved": True,
                },
                {
                    "file_id": common_file.id,
                    "field_name": "memorykeeper_source_capture_year_basis",
                    "old_value": old_basis,
                    "new_value": SOURCE_BASIS,
                    "source": HISTORY_SOURCE,
                    "priority": MetadataPriority.SYSTEM,
                    "modified_by": self.__class__.__name__,
                    "approved": True,
                },
            ],
            commit=False,
        )
        self.changes.append(
            service_name=SERVICE_NAME,
            resource_type=RESOURCE_TYPE,
            resource_id=common_file.file_id,
            operation=ChangeOperation.UPDATE,
            revision=state.revision,
        )
        self.db.flush()
        self.db.refresh(state)
        if not self._state_matches_target(state, target):
            raise ReconciliationApplyError(
                f"{plan.sha256}: persisted projection differs from target"
            )
        if (state.user_capture_datetime, state.user_capture_precision) != user_before:
            raise ReconciliationApplyError(f"{plan.sha256}: USER fields changed")
        original_after = (
            context.metadata.original_capture_datetime
            if context.metadata is not None
            else None
        )
        if original_after != original_before:
            raise ReconciliationApplyError(f"{plan.sha256}: EXIF metadata changed")

    @staticmethod
    def _result(
        plan: ReconciliationPlanRow,
        context: LoadedContext | None,
        status: str,
        message: str,
    ) -> VerificationResult:
        common_file = context.common_file if context is not None else None
        state = context.state if context is not None else None
        return VerificationResult(
            plan=plan,
            file_id=common_file.id if common_file is not None else None,
            verification_status=status,
            current_date_basis=state.date_basis if state is not None else None,
            current_effective_year=(
                state.effective_capture_year if state is not None else None
            ),
            current_effective_date=(
                state.effective_capture_date if state is not None else None
            ),
            current_effective_datetime=(
                state.effective_capture_datetime if state is not None else None
            ),
            current_precision=(
                state.effective_capture_precision if state is not None else None
            ),
            current_revision=(int(state.revision or 0) if state is not None else None),
            message=message,
        )


def print_summary(results: Sequence[VerificationResult], stream: TextIO) -> None:
    counts = Counter(result.verification_status for result in results)
    year_only_total = sum(counts[status] for status in YEAR_ONLY_READY_STATUSES)
    ready_total = sum(counts[status] for status in READY_STATUSES)
    print(f"plan_rows={len(results)}", file=stream)
    print(
        f"db_verified={len(results) - counts[PLAN_EXCLUDED]}",
        file=stream,
    )
    print(file=stream)
    for status in (
        READY_PROVENANCE_ONLY_EXIF,
        READY_YEAR_ONLY_EXIF_CONFLICT,
        READY_YEAR_ONLY_IMPORTED,
        READY_YEAR_ONLY_CREATED,
    ):
        print(f"{status}={counts[status]}", file=stream)
    print(f"READY_WRITE_TOTAL={ready_total}", file=stream)
    print(file=stream)
    print(f"KEEP_EXACT_USER={counts[KEEP_USER]}", file=stream)
    print(f"USER_CONFLICT_REVIEW={counts[USER_CONFLICT_REVIEW]}", file=stream)
    print(file=stream)
    for status in (
        ALREADY_APPLIED,
        STALE_REVISION,
        MISSING_FILE,
        MISSING_MEMORYKEEPER_LINK,
        MISSING_STATE,
        SNAPSHOT_MISMATCH,
        PROJECTION_CONFLICT,
        OTHER_CONFLICT,
        PLAN_EXCLUDED,
    ):
        print(f"{status}={counts[status]}", file=stream)
    print(f"DB_VERIFIED_YEAR_ONLY_TOTAL={year_only_total}", file=stream)


def write_report_csv(
    results: Sequence[VerificationResult],
    output_path: Path,
) -> None:
    with output_path.open("x", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=REPORT_CSV_FIELDS)
        writer.writeheader()
        for result in sorted(results, key=lambda item: _plan_sort_key(item.plan)):
            writer.writerow(result.csv_dict())


def has_blocking_conflicts(results: Iterable[VerificationResult]) -> bool:
    return any(result.verification_status in BLOCKING_STATUSES for result in results)


def _parse_plan_row(
    values: dict[str, str | None],
    *,
    row_number: int,
) -> ReconciliationPlanRow:
    action = _required_text(values.get("action"), row_number, "action", upper=True)
    reason = _required_text(values.get("reason"), row_number, "reason", upper=True)
    review_reason = _text_value(values.get("review_reason"), upper=True)
    sha256 = _text_value(values.get("sha256"), lower=True)
    source_year = _integer(values.get("original_path_year"), row_number, "original_path_year")

    if action == EXCLUDE:
        if review_reason is not None:
            raise ReconciliationValidationError(
                f"row {row_number}: excluded row cannot have review_reason"
            )
    else:
        allowed_reasons = _ACTION_REASON.get(action)
        if allowed_reasons is None or reason not in allowed_reasons:
            raise ReconciliationValidationError(
                f"row {row_number}: invalid action/reason {action}/{reason}"
            )
        if sha256 is None or _SHA256.fullmatch(sha256) is None:
            raise ReconciliationValidationError(
                f"row {row_number}: actionable row requires valid sha256"
            )
        if source_year is None or not 1 <= source_year <= 9999:
            raise ReconciliationValidationError(
                f"row {row_number}: actionable row requires valid source year"
            )
    if sha256 is not None and _SHA256.fullmatch(sha256) is None:
        raise ReconciliationValidationError(f"row {row_number}: invalid sha256")
    if review_reason not in {None, USER_SOURCE_YEAR_CONFLICT}:
        raise ReconciliationValidationError(
            f"row {row_number}: invalid review_reason {review_reason}"
        )
    if review_reason is not None and action != KEEP_EXACT_USER:
        raise ReconciliationValidationError(
            f"row {row_number}: review_reason requires KEEP_EXACT_USER"
        )

    current_date_basis = _text_value(values.get("current_date_basis"), upper=True)
    current_effective_year = _integer(
        values.get("current_effective_capture_year"),
        row_number,
        "current_effective_capture_year",
    )
    current_effective_date = _date_value(
        values.get("current_effective_capture_date"),
        row_number,
    )
    current_effective_datetime = _datetime_value(
        values.get("current_effective_capture_datetime"),
        row_number,
        "current_effective_capture_datetime",
    )
    current_user_datetime = _datetime_value(
        values.get("current_user_capture_datetime"),
        row_number,
        "current_user_capture_datetime",
    )
    current_user_precision = _text_value(
        values.get("current_user_capture_precision"), upper=True
    )
    current_revision = _integer(
        values.get("current_date_revision"), row_number, "current_date_revision"
    )
    source_path_count = _integer(
        values.get("source_path_count"), row_number, "source_path_count"
    )

    if action != EXCLUDE:
        if action == KEEP_EXACT_EXIF:
            expected_basis = CaptureDateBasis.EXIF
        elif action == KEEP_EXACT_USER:
            expected_basis = CaptureDateBasis.USER
        else:
            expected_basis = {
                EXIF_YEAR_CONFLICT: CaptureDateBasis.EXIF,
                IMPORTED_FALLBACK: CaptureDateBasis.IMPORTED,
                CREATED_FALLBACK: CaptureDateBasis.CREATED,
            }[reason]
        if current_date_basis != expected_basis:
            raise ReconciliationValidationError(
                f"row {row_number}: {action}/{reason} requires "
                f"current_date_basis={expected_basis}"
            )
        if current_revision is None or current_revision < 0:
            raise ReconciliationValidationError(
                f"row {row_number}: actionable row requires non-negative revision"
            )
        if source_path_count is None or source_path_count < 1:
            raise ReconciliationValidationError(
                f"row {row_number}: actionable row requires positive source_path_count"
            )
        if (
            current_effective_year is None
            or current_effective_date is None
            or current_effective_datetime is None
        ):
            raise ReconciliationValidationError(
                f"row {row_number}: actionable exact-date snapshot is incomplete"
            )
        if current_effective_datetime.date() != current_effective_date:
            raise ReconciliationValidationError(
                f"row {row_number}: effective date contradicts effective datetime"
            )
        if current_effective_datetime.year != current_effective_year:
            raise ReconciliationValidationError(
                f"row {row_number}: effective year contradicts effective datetime"
            )
        if action == KEEP_EXACT_EXIF and current_effective_year != source_year:
            raise ReconciliationValidationError(
                f"row {row_number}: KEEP_EXACT_EXIF year must match source year"
            )
        if action == SET_YEAR_ONLY and reason == EXIF_YEAR_CONFLICT:
            if current_effective_year == source_year:
                raise ReconciliationValidationError(
                    f"row {row_number}: EXIF conflict year must differ from source year"
                )
        if action == KEEP_EXACT_USER:
            if current_user_datetime is None or current_user_precision is None:
                raise ReconciliationValidationError(
                    f"row {row_number}: KEEP_EXACT_USER requires USER snapshot"
                )
            if current_user_datetime != current_effective_datetime:
                raise ReconciliationValidationError(
                    f"row {row_number}: USER and effective datetimes must match"
                )
            expected_review = (
                USER_SOURCE_YEAR_CONFLICT
                if current_user_datetime.year != source_year
                else None
            )
            if review_reason != expected_review:
                raise ReconciliationValidationError(
                    f"row {row_number}: USER review_reason contradicts source year"
                )

    return ReconciliationPlanRow(
        row_number=row_number,
        sha256=sha256,
        source_year=source_year,
        action=action,
        reason=reason,
        review_reason=review_reason,
        current_date_basis=current_date_basis,
        current_effective_year=current_effective_year,
        current_effective_date=current_effective_date,
        current_effective_datetime=current_effective_datetime,
        current_user_datetime=current_user_datetime,
        current_user_precision=current_user_precision,
        current_revision=current_revision,
        source_path_count=source_path_count or 0,
    )


def _ready_status(plan: ReconciliationPlanRow) -> str:
    if plan.action == KEEP_EXACT_EXIF:
        return READY_PROVENANCE_ONLY_EXIF
    return {
        EXIF_YEAR_CONFLICT: READY_YEAR_ONLY_EXIF_CONFLICT,
        IMPORTED_FALLBACK: READY_YEAR_ONLY_IMPORTED,
        CREATED_FALLBACK: READY_YEAR_ONLY_CREATED,
    }[plan.reason]


def _validate_batch_size(batch_size: int) -> None:
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")


def _chunks(items: Sequence, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _plan_sort_key(row: ReconciliationPlanRow) -> tuple[object, ...]:
    return (row.sha256 is None, row.sha256 or "", row.reason, row.row_number)


def _text_value(
    value: str | None,
    *,
    upper: bool = False,
    lower: bool = False,
) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    if upper:
        return normalized.upper()
    if lower:
        return normalized.lower()
    return normalized


def _required_text(
    value: str | None,
    row_number: int,
    field_name: str,
    *,
    upper: bool = False,
) -> str:
    normalized = _text_value(value, upper=upper)
    if normalized is None:
        raise ReconciliationValidationError(
            f"row {row_number}: {field_name} is required"
        )
    return normalized


def _integer(value: str | None, row_number: int, field_name: str) -> int | None:
    normalized = _text_value(value)
    if normalized is None:
        return None
    try:
        return int(normalized)
    except ValueError as exc:
        raise ReconciliationValidationError(
            f"row {row_number}: {field_name} must be an integer"
        ) from exc


def _date_value(value: str | None, row_number: int) -> date | None:
    normalized = _text_value(value)
    if normalized is None:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise ReconciliationValidationError(
            f"row {row_number}: current_effective_capture_date is invalid"
        ) from exc


def _datetime_value(
    value: str | None,
    row_number: int,
    field_name: str,
) -> datetime | None:
    normalized = _text_value(value)
    if normalized is None:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ReconciliationValidationError(
            f"row {row_number}: {field_name} is invalid"
        ) from exc
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        raise ReconciliationValidationError(
            f"row {row_number}: {field_name} must be timezone-naive"
        )
    return parsed


def _prepare_report_path(plan_path: Path, report: Path | None) -> Path | None:
    if report is None:
        return None
    resolved = report.resolve(strict=False)
    if resolved == plan_path:
        raise ValueError("plan and report CSV paths must differ")
    if not resolved.parent.is_dir():
        raise ValueError(f"report parent does not exist: {resolved.parent}")
    if resolved.exists():
        raise FileExistsError(f"report CSV already exists: {resolved}")
    return resolved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify and optionally apply MemoryKeeper source-year reconciliation"
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _validate_batch_size(args.batch_size)
    plan_path = args.plan.resolve(strict=True)
    report_path = _prepare_report_path(plan_path, args.report)
    plans = load_plan_csv(plan_path)

    from app.common.database import SessionLocal

    db = SessionLocal()
    try:
        if not args.apply and db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        reconciler = MemoryKeeperSourceYearReconciler(db)
        results = reconciler.verify_all(plans)
        print_summary(results, sys.stdout)
        if report_path is not None:
            write_report_csv(results, report_path)
            print(f"report_csv={report_path}")
        if not args.apply:
            db.rollback()
            print("mode=dry-run")
            return 0
        if has_blocking_conflicts(results):
            db.rollback()
            print("apply_refused=blocking_conflicts")
            return 2

        db.rollback()
        stats = reconciler.apply_verified(results, batch_size=args.batch_size)
        print("mode=apply")
        print(f"applied={stats.applied}")
        print(
            "already_applied_during_lock="
            f"{stats.already_applied_during_lock}"
        )
        print(f"committed_batches={stats.committed_batches}")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
