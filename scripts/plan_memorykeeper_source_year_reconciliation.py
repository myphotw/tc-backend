"""Build a read-only MemoryKeeper source-year reconciliation plan from audit CSV."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import re
import sys
from typing import Iterable, TextIO

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_memorykeeper_original_years import (  # noqa: E402
    AMBIGUOUS_SOURCE_YEAR,
    CSV_FIELDS as AUDIT_CSV_FIELDS,
    HASH_ERROR,
    MATCH,
    MISSING_EFFECTIVE_YEAR,
    NOT_IN_MEMORYKEEPER,
    NO_PATH_YEAR,
    YEAR_MISMATCH,
)


KEEP_EXACT_EXIF = "KEEP_EXACT_EXIF"
KEEP_EXACT_USER = "KEEP_EXACT_USER"
SET_YEAR_ONLY = "SET_YEAR_ONLY"
EXCLUDE = "EXCLUDE"

EXIF_SOURCE_YEAR_MATCH = "EXIF_SOURCE_YEAR_MATCH"
USER_EXACT = "USER_EXACT"
EXIF_YEAR_CONFLICT = "EXIF_YEAR_CONFLICT"
IMPORTED_FALLBACK = "IMPORTED_FALLBACK"
CREATED_FALLBACK = "CREATED_FALLBACK"
USER_SOURCE_YEAR_CONFLICT = "USER_SOURCE_YEAR_CONFLICT"

PLAN_CSV_FIELDS = (
    "sha256",
    "original_path_year",
    "action",
    "reason",
    "review_reason",
    "current_date_basis",
    "current_effective_capture_year",
    "current_effective_capture_date",
    "current_effective_capture_datetime",
    "current_user_capture_datetime",
    "current_user_capture_precision",
    "current_date_revision",
    "source_path_count",
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_MEMORYKEEPER_STATUSES = frozenset(
    {MATCH, YEAR_MISMATCH, MISSING_EFFECTIVE_YEAR}
)
_EXCLUDED_WITHOUT_SHA = frozenset({NO_PATH_YEAR, HASH_ERROR})
_KNOWN_STATUSES = frozenset(
    {
        MATCH,
        YEAR_MISMATCH,
        MISSING_EFFECTIVE_YEAR,
        NOT_IN_MEMORYKEEPER,
        NO_PATH_YEAR,
        AMBIGUOUS_SOURCE_YEAR,
        HASH_ERROR,
    }
)


class PlanValidationError(ValueError):
    """Raised when the audit snapshot cannot produce an unambiguous plan."""


@dataclass(frozen=True)
class AuditCsvRow:
    row_number: int
    status: str
    original_path_year: int | None
    effective_capture_year: int | None
    effective_capture_date: str | None
    effective_capture_datetime: str | None
    date_basis: str | None
    user_capture_datetime: str | None
    user_capture_precision: str | None
    sha256: str | None
    date_revision: int | None

    @property
    def snapshot(self) -> tuple[object, ...]:
        return (
            self.date_basis,
            self.effective_capture_year,
            self.effective_capture_date,
            self.effective_capture_datetime,
            self.user_capture_datetime,
            self.user_capture_precision,
            self.date_revision,
        )


@dataclass(frozen=True)
class PlanRow:
    sha256: str | None
    original_path_year: int | None
    action: str
    reason: str
    review_reason: str | None
    current_date_basis: str | None
    current_effective_capture_year: int | None
    current_effective_capture_date: str | None
    current_effective_capture_datetime: str | None
    current_user_capture_datetime: str | None
    current_user_capture_precision: str | None
    current_date_revision: int | None
    source_path_count: int

    def csv_dict(self) -> dict[str, object]:
        return {
            "sha256": self.sha256 or "",
            "original_path_year": self.original_path_year or "",
            "action": self.action,
            "reason": self.reason,
            "review_reason": self.review_reason or "",
            "current_date_basis": self.current_date_basis or "",
            "current_effective_capture_year": (
                self.current_effective_capture_year or ""
            ),
            "current_effective_capture_date": (
                self.current_effective_capture_date or ""
            ),
            "current_effective_capture_datetime": (
                self.current_effective_capture_datetime or ""
            ),
            "current_user_capture_datetime": (
                self.current_user_capture_datetime or ""
            ),
            "current_user_capture_precision": (
                self.current_user_capture_precision or ""
            ),
            "current_date_revision": (
                self.current_date_revision
                if self.current_date_revision is not None
                else ""
            ),
            "source_path_count": self.source_path_count,
        }


@dataclass(frozen=True)
class ReconciliationPlan:
    rows: tuple[PlanRow, ...]
    total_audit_rows: int
    unique_memorykeeper_sha: int
    counts: Counter[str]
    year_only_by_source_year: Counter[int]
    year_only_by_reason: Counter[str]


def load_audit_csv(path: Path) -> list[AuditCsvRow]:
    """Load and strictly validate the existing audit CSV without modifying it."""
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        missing = set(AUDIT_CSV_FIELDS) - set(reader.fieldnames or ())
        if missing:
            raise PlanValidationError(
                "audit CSV is missing required columns: " + ", ".join(sorted(missing))
            )
        return [
            _parse_audit_row(values, row_number=row_number)
            for row_number, values in enumerate(reader, start=2)
        ]


def build_plan(audit_rows: Iterable[AuditCsvRow]) -> ReconciliationPlan:
    """Classify a CSV snapshot into one deterministic, SHA-deduplicated plan."""
    rows = list(audit_rows)
    grouped: dict[str, list[AuditCsvRow]] = defaultdict(list)
    standalone_exclusions: list[AuditCsvRow] = []
    for row in rows:
        if row.sha256 is None:
            if row.status not in _EXCLUDED_WITHOUT_SHA:
                raise PlanValidationError(
                    f"row {row.row_number}: {row.status} requires sha256"
                )
            standalone_exclusions.append(row)
            continue
        grouped[row.sha256].append(row)

    plan_rows: list[PlanRow] = []
    counts: Counter[str] = Counter()
    year_counts: Counter[int] = Counter()
    reason_counts: Counter[str] = Counter()
    unique_memorykeeper_sha = 0

    for sha256 in sorted(grouped):
        duplicates = grouped[sha256]
        _require_consistent_snapshot(sha256, duplicates)
        statuses = {row.status for row in duplicates}
        years = {row.original_path_year for row in duplicates}
        if None in years:
            raise PlanValidationError(f"sha256 {sha256}: source year is missing")
        if len(years) > 1 or AMBIGUOUS_SOURCE_YEAR in statuses:
            plan_rows.append(
                _to_plan_row(
                    duplicates[0],
                    action=EXCLUDE,
                    reason=AMBIGUOUS_SOURCE_YEAR,
                    original_path_year=None,
                    source_path_count=len(duplicates),
                )
            )
            counts[AMBIGUOUS_SOURCE_YEAR] += 1
            continue
        if len(statuses) != 1:
            raise PlanValidationError(
                f"sha256 {sha256}: inconsistent statuses {sorted(statuses)}"
            )

        row = duplicates[0]
        source_year = next(iter(years))
        if row.status in _MEMORYKEEPER_STATUSES:
            unique_memorykeeper_sha += 1
        if row.status in {NOT_IN_MEMORYKEEPER, MISSING_EFFECTIVE_YEAR}:
            plan_rows.append(
                _to_plan_row(
                    row,
                    action=EXCLUDE,
                    reason=row.status,
                    source_path_count=len(duplicates),
                )
            )
            counts[row.status] += 1
            continue
        if row.status not in {MATCH, YEAR_MISMATCH}:
            raise PlanValidationError(
                f"sha256 {sha256}: unsupported grouped status {row.status}"
            )

        _validate_snapshot_status(row, source_year=source_year)
        plan_row = _classify_memorykeeper_row(
            row,
            source_year=source_year,
            source_path_count=len(duplicates),
        )
        plan_rows.append(plan_row)
        counts[plan_row.action] += 1
        if plan_row.action == KEEP_EXACT_EXIF:
            counts["SOURCE_PROVENANCE_ONLY_EXIF"] += 1
        if plan_row.review_reason == USER_SOURCE_YEAR_CONFLICT:
            counts[USER_SOURCE_YEAR_CONFLICT] += 1
        if plan_row.action == SET_YEAR_ONLY:
            year_counts[source_year] += 1
            reason_counts[plan_row.reason] += 1

    for row in standalone_exclusions:
        plan_rows.append(
            _to_plan_row(
                row,
                action=EXCLUDE,
                reason=row.status,
                source_path_count=1,
            )
        )
        counts[row.status] += 1

    plan_rows.sort(key=_plan_sort_key)
    total_year_only = counts[SET_YEAR_ONLY]
    if sum(year_counts.values()) != total_year_only:
        raise PlanValidationError("source-year totals do not match SET_YEAR_ONLY")
    if sum(reason_counts.values()) != total_year_only:
        raise PlanValidationError("reason totals do not match SET_YEAR_ONLY")

    return ReconciliationPlan(
        rows=tuple(plan_rows),
        total_audit_rows=len(rows),
        unique_memorykeeper_sha=unique_memorykeeper_sha,
        counts=counts,
        year_only_by_source_year=year_counts,
        year_only_by_reason=reason_counts,
    )


def write_plan_csv(plan: ReconciliationPlan, output_path: Path) -> None:
    """Write a new plan CSV without replacing an existing artifact."""
    with output_path.open("x", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=PLAN_CSV_FIELDS)
        writer.writeheader()
        for row in plan.rows:
            writer.writerow(row.csv_dict())


def print_summary(plan: ReconciliationPlan, stream: TextIO) -> None:
    counts = plan.counts
    total_year_only = counts[SET_YEAR_ONLY]
    print(f"total_audit_rows={plan.total_audit_rows}", file=stream)
    print(f"unique_memorykeeper_sha={plan.unique_memorykeeper_sha}", file=stream)
    print(file=stream)
    print(f"KEEP_EXACT_EXIF={counts[KEEP_EXACT_EXIF]}", file=stream)
    print(f"KEEP_EXACT_USER={counts[KEEP_EXACT_USER]}", file=stream)
    print(file=stream)
    print(
        "SOURCE_PROVENANCE_ONLY_EXIF="
        f"{counts['SOURCE_PROVENANCE_ONLY_EXIF']}",
        file=stream,
    )
    print(file=stream)
    print(
        f"TO_YEAR_ONLY_EXIF_CONFLICT={plan.year_only_by_reason[EXIF_YEAR_CONFLICT]}",
        file=stream,
    )
    print(
        f"TO_YEAR_ONLY_IMPORTED={plan.year_only_by_reason[IMPORTED_FALLBACK]}",
        file=stream,
    )
    print(
        f"TO_YEAR_ONLY_CREATED={plan.year_only_by_reason[CREATED_FALLBACK]}",
        file=stream,
    )
    print(f"TO_YEAR_ONLY_TOTAL={total_year_only}", file=stream)
    print(file=stream)
    print(
        "AUTO_RECONCILE_WRITE_TOTAL="
        f"{counts['SOURCE_PROVENANCE_ONLY_EXIF'] + total_year_only}",
        file=stream,
    )
    print(file=stream)
    print(
        "USER_SOURCE_YEAR_CONFLICT_REVIEW="
        f"{counts[USER_SOURCE_YEAR_CONFLICT]}",
        file=stream,
    )
    print(file=stream)
    for status in (
        NOT_IN_MEMORYKEEPER,
        NO_PATH_YEAR,
        AMBIGUOUS_SOURCE_YEAR,
        HASH_ERROR,
        MISSING_EFFECTIVE_YEAR,
    ):
        print(f"{status}={counts[status]}", file=stream)
    print("YEAR_ONLY_BY_SOURCE_YEAR", file=stream)
    for year in sorted(plan.year_only_by_source_year):
        print(f"{year}={plan.year_only_by_source_year[year]}", file=stream)
    print("YEAR_ONLY_BY_REASON", file=stream)
    for reason in (EXIF_YEAR_CONFLICT, IMPORTED_FALLBACK, CREATED_FALLBACK):
        print(f"{reason}={plan.year_only_by_reason[reason]}", file=stream)


def _parse_audit_row(
    values: dict[str, str | None],
    *,
    row_number: int,
) -> AuditCsvRow:
    status = _text(values.get("status"), upper=True)
    if status not in _KNOWN_STATUSES:
        raise PlanValidationError(f"row {row_number}: unknown status {status!r}")
    sha256 = _text(values.get("sha256"), lower=True)
    if sha256 is not None and _SHA256.fullmatch(sha256) is None:
        raise PlanValidationError(f"row {row_number}: invalid sha256")
    source_year = _integer(values.get("original_path_year"), row_number, "original_path_year")
    if source_year is not None and not 1 <= source_year <= 9999:
        raise PlanValidationError(f"row {row_number}: invalid original_path_year")
    return AuditCsvRow(
        row_number=row_number,
        status=status,
        original_path_year=source_year,
        effective_capture_year=_integer(
            values.get("effective_capture_year"),
            row_number,
            "effective_capture_year",
        ),
        effective_capture_date=_date_text(
            values.get("effective_capture_date"), row_number
        ),
        effective_capture_datetime=_datetime_text(
            values.get("effective_capture_datetime"),
            row_number,
            "effective_capture_datetime",
        ),
        date_basis=_text(values.get("date_basis"), upper=True),
        user_capture_datetime=_datetime_text(
            values.get("user_capture_datetime"),
            row_number,
            "user_capture_datetime",
        ),
        user_capture_precision=_text(
            values.get("user_capture_precision"), upper=True
        ),
        sha256=sha256,
        date_revision=_integer(values.get("date_revision"), row_number, "date_revision"),
    )


def _require_consistent_snapshot(
    sha256: str,
    rows: list[AuditCsvRow],
) -> None:
    snapshots = {row.snapshot for row in rows}
    if len(snapshots) != 1:
        row_numbers = ", ".join(str(row.row_number) for row in rows)
        raise PlanValidationError(
            f"sha256 {sha256}: inconsistent snapshots at rows {row_numbers}"
        )


def _validate_snapshot_status(row: AuditCsvRow, *, source_year: int) -> None:
    if row.effective_capture_year is None:
        raise PlanValidationError(
            f"row {row.row_number}: eligible row has no effective_capture_year"
        )
    expected = MATCH if row.effective_capture_year == source_year else YEAR_MISMATCH
    if row.status != expected:
        raise PlanValidationError(
            f"row {row.row_number}: status {row.status} contradicts capture year"
        )
    if (
        row.effective_capture_date is None
        or row.effective_capture_datetime is None
        or row.date_revision is None
    ):
        raise PlanValidationError(
            f"row {row.row_number}: eligible exact-date snapshot is incomplete"
        )


def _classify_memorykeeper_row(
    row: AuditCsvRow,
    *,
    source_year: int,
    source_path_count: int,
) -> PlanRow:
    if row.date_basis == "EXIF":
        if row.effective_capture_year == source_year:
            return _to_plan_row(
                row,
                action=KEEP_EXACT_EXIF,
                reason=EXIF_SOURCE_YEAR_MATCH,
                source_path_count=source_path_count,
            )
        return _to_plan_row(
            row,
            action=SET_YEAR_ONLY,
            reason=EXIF_YEAR_CONFLICT,
            source_path_count=source_path_count,
        )
    if row.date_basis == "IMPORTED":
        return _to_plan_row(
            row,
            action=SET_YEAR_ONLY,
            reason=IMPORTED_FALLBACK,
            source_path_count=source_path_count,
        )
    if row.date_basis == "CREATED":
        return _to_plan_row(
            row,
            action=SET_YEAR_ONLY,
            reason=CREATED_FALLBACK,
            source_path_count=source_path_count,
        )
    if row.date_basis == "USER":
        if row.user_capture_datetime is None:
            raise PlanValidationError(
                f"row {row.row_number}: USER basis has no user_capture_datetime"
            )
        user_year = datetime.fromisoformat(row.user_capture_datetime).year
        if user_year != row.effective_capture_year:
            raise PlanValidationError(
                f"row {row.row_number}: USER year contradicts effective year"
            )
        return _to_plan_row(
            row,
            action=KEEP_EXACT_USER,
            reason=USER_EXACT,
            review_reason=(
                USER_SOURCE_YEAR_CONFLICT if user_year != source_year else None
            ),
            source_path_count=source_path_count,
        )
    raise PlanValidationError(
        f"row {row.row_number}: unsupported date_basis {row.date_basis!r}"
    )


def _to_plan_row(
    row: AuditCsvRow,
    *,
    action: str,
    reason: str,
    review_reason: str | None = None,
    original_path_year: int | None = None,
    source_path_count: int,
) -> PlanRow:
    return PlanRow(
        sha256=row.sha256,
        original_path_year=(
            row.original_path_year
            if original_path_year is None and reason != AMBIGUOUS_SOURCE_YEAR
            else original_path_year
        ),
        action=action,
        reason=reason,
        review_reason=review_reason,
        current_date_basis=row.date_basis,
        current_effective_capture_year=row.effective_capture_year,
        current_effective_capture_date=row.effective_capture_date,
        current_effective_capture_datetime=row.effective_capture_datetime,
        current_user_capture_datetime=row.user_capture_datetime,
        current_user_capture_precision=row.user_capture_precision,
        current_date_revision=row.date_revision,
        source_path_count=source_path_count,
    )


def _plan_sort_key(row: PlanRow) -> tuple[object, ...]:
    return (
        row.sha256 is None,
        row.sha256 or "",
        row.original_path_year or 0,
        row.reason,
    )


def _text(
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


def _integer(value: str | None, row_number: int, field_name: str) -> int | None:
    normalized = _text(value)
    if normalized is None:
        return None
    try:
        return int(normalized)
    except ValueError as exc:
        raise PlanValidationError(
            f"row {row_number}: {field_name} must be an integer"
        ) from exc


def _date_text(value: str | None, row_number: int) -> str | None:
    normalized = _text(value)
    if normalized is None:
        return None
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise PlanValidationError(
            f"row {row_number}: effective_capture_date is invalid"
        ) from exc


def _datetime_text(
    value: str | None,
    row_number: int,
    field_name: str,
) -> str | None:
    normalized = _text(value)
    if normalized is None:
        return None
    try:
        return datetime.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise PlanValidationError(
            f"row {row_number}: {field_name} is invalid"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a CSV-only MemoryKeeper source-year reconciliation plan"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_path = args.input.resolve(strict=True)
    output_path = args.output.resolve(strict=False)
    if input_path == output_path:
        raise ValueError("input and output CSV paths must differ")
    if not output_path.parent.is_dir():
        raise ValueError(f"output parent does not exist: {output_path.parent}")

    plan = build_plan(load_audit_csv(input_path))
    write_plan_csv(plan, output_path)
    print_summary(plan, stream=sys.stdout)
    print(f"plan_csv={output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
