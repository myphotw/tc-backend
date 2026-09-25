from __future__ import annotations

import csv
from pathlib import Path

import pytest

from scripts.audit_memorykeeper_original_years import (
    AMBIGUOUS_SOURCE_YEAR,
    CSV_FIELDS,
    HASH_ERROR,
    MATCH,
    MISSING_EFFECTIVE_YEAR,
    NOT_IN_MEMORYKEEPER,
    NO_PATH_YEAR,
    YEAR_MISMATCH,
)
from scripts.plan_memorykeeper_source_year_reconciliation import (
    CREATED_FALLBACK,
    EXCLUDE,
    EXIF_YEAR_CONFLICT,
    IMPORTED_FALLBACK,
    KEEP_EXACT_EXIF,
    KEEP_EXACT_USER,
    PLAN_CSV_FIELDS,
    SET_YEAR_ONLY,
    USER_SOURCE_YEAR_CONFLICT,
    PlanValidationError,
    build_plan,
    load_audit_csv,
    main,
)


def _sha(character: str) -> str:
    return character * 64


def _row(
    sha256: str | None,
    source_year: int | None,
    *,
    basis: str | None = None,
    effective_year: int | None = None,
    status: str | None = None,
    user_year: int | None = None,
    path: str = "source.jpg",
    revision: int | None = 0,
) -> dict[str, object]:
    if status is None:
        status = MATCH if source_year == effective_year else YEAR_MISMATCH
    effective_date = (
        f"{effective_year:04d}-06-15" if effective_year is not None else ""
    )
    effective_datetime = (
        f"{effective_year:04d}-06-15T12:30:00"
        if effective_year is not None
        else ""
    )
    values: dict[str, object] = {name: "" for name in CSV_FIELDS}
    values.update(
        {
            "status": status,
            "original_path_year": source_year or "",
            "effective_capture_year": effective_year or "",
            "effective_capture_date": effective_date,
            "effective_capture_datetime": effective_datetime,
            "date_basis": basis or "",
            "user_capture_datetime": (
                f"{user_year:04d}-06-15T00:00:00"
                if user_year is not None
                else ""
            ),
            "user_capture_precision": "DATE" if user_year is not None else "",
            "sha256": sha256 or "",
            "original_path": path,
            "original_filename": Path(path).name,
            "date_revision": revision if revision is not None else "",
            "source_duplicate_count": 1,
        }
    )
    return values


def _write_audit(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _build(path: Path, rows: list[dict[str, object]]):
    _write_audit(path, rows)
    return build_plan(load_audit_csv(path))


def test_plan_classifies_all_policy_paths_and_deduplicates(tmp_path: Path) -> None:
    exif_match = _row(_sha("a"), 2018, basis="EXIF", effective_year=2018)
    exif_duplicate = dict(exif_match)
    exif_duplicate["original_path"] = "copy.jpg"
    exif_duplicate["original_filename"] = "copy.jpg"
    exif_duplicate["source_duplicate_count"] = 2
    exif_match["source_duplicate_count"] = 2

    rows = [
        exif_match,
        exif_duplicate,
        _row(_sha("b"), 2020, basis="EXIF", effective_year=2013),
        _row(_sha("c"), 2021, basis="IMPORTED", effective_year=2021),
        _row(_sha("d"), 2022, basis="IMPORTED", effective_year=2026),
        _row(_sha("e"), 2023, basis="CREATED", effective_year=2023),
        _row(_sha("f"), 2024, basis="CREATED", effective_year=2026),
        _row(
            _sha("1"),
            2024,
            basis="USER",
            effective_year=2024,
            user_year=2024,
        ),
        _row(
            _sha("2"),
            2024,
            basis="USER",
            effective_year=2025,
            user_year=2025,
        ),
        _row(
            _sha("3"),
            2020,
            status=NOT_IN_MEMORYKEEPER,
            revision=None,
        ),
        _row(
            _sha("4"),
            2020,
            status=MISSING_EFFECTIVE_YEAR,
            revision=None,
        ),
        _row(None, None, status=NO_PATH_YEAR, revision=None),
        _row(None, 2020, status=HASH_ERROR, revision=None),
    ]

    plan = _build(tmp_path / "audit.csv", rows)
    by_sha = {row.sha256: row for row in plan.rows if row.sha256}

    assert by_sha[_sha("a")].action == KEEP_EXACT_EXIF
    assert by_sha[_sha("a")].source_path_count == 2
    assert by_sha[_sha("b")].action == SET_YEAR_ONLY
    assert by_sha[_sha("b")].reason == EXIF_YEAR_CONFLICT
    assert by_sha[_sha("c")].reason == IMPORTED_FALLBACK
    assert by_sha[_sha("d")].reason == IMPORTED_FALLBACK
    assert by_sha[_sha("e")].reason == CREATED_FALLBACK
    assert by_sha[_sha("f")].reason == CREATED_FALLBACK
    assert by_sha[_sha("1")].action == KEEP_EXACT_USER
    assert by_sha[_sha("1")].review_reason is None
    assert by_sha[_sha("2")].action == KEEP_EXACT_USER
    assert by_sha[_sha("2")].review_reason == USER_SOURCE_YEAR_CONFLICT
    assert by_sha[_sha("3")].action == EXCLUDE
    assert by_sha[_sha("4")].action == EXCLUDE

    assert plan.unique_memorykeeper_sha == 9
    assert plan.counts[KEEP_EXACT_EXIF] == 1
    assert plan.counts[KEEP_EXACT_USER] == 2
    assert plan.counts["SOURCE_PROVENANCE_ONLY_EXIF"] == 1
    assert plan.counts[SET_YEAR_ONLY] == 5
    assert plan.counts[USER_SOURCE_YEAR_CONFLICT] == 1
    assert plan.counts[NOT_IN_MEMORYKEEPER] == 1
    assert plan.counts[MISSING_EFFECTIVE_YEAR] == 1
    assert plan.counts[NO_PATH_YEAR] == 1
    assert plan.counts[HASH_ERROR] == 1
    assert sum(plan.year_only_by_source_year.values()) == 5
    assert plan.year_only_by_reason == {
        EXIF_YEAR_CONFLICT: 1,
        IMPORTED_FALLBACK: 2,
        CREATED_FALLBACK: 2,
    }


def test_different_source_years_for_one_sha_are_excluded(tmp_path: Path) -> None:
    duplicate_sha = _sha("a")
    rows = [
        _row(
            duplicate_sha,
            2020,
            basis="EXIF",
            effective_year=2020,
            status=AMBIGUOUS_SOURCE_YEAR,
        ),
        _row(
            duplicate_sha,
            2021,
            basis="EXIF",
            effective_year=2020,
            status=AMBIGUOUS_SOURCE_YEAR,
        ),
    ]

    plan = _build(tmp_path / "audit.csv", rows)

    assert len(plan.rows) == 1
    assert plan.rows[0].action == EXCLUDE
    assert plan.rows[0].reason == AMBIGUOUS_SOURCE_YEAR
    assert plan.rows[0].original_path_year is None
    assert plan.rows[0].source_path_count == 2
    assert plan.counts[AMBIGUOUS_SOURCE_YEAR] == 1


def test_same_sha_with_inconsistent_snapshot_fails_closed(tmp_path: Path) -> None:
    first = _row(_sha("a"), 2020, basis="EXIF", effective_year=2020)
    second = dict(first)
    second["effective_capture_date"] = "2020-07-01"

    audit_path = tmp_path / "audit.csv"
    _write_audit(audit_path, [first, second])

    with pytest.raises(PlanValidationError, match="inconsistent snapshots"):
        build_plan(load_audit_csv(audit_path))


@pytest.mark.parametrize(
    ("basis", "source_year", "effective_year", "expected_reason"),
    [
        ("IMPORTED", 2020, 2020, IMPORTED_FALLBACK),
        ("IMPORTED", 2020, 2026, IMPORTED_FALLBACK),
        ("CREATED", 2020, 2020, CREATED_FALLBACK),
        ("CREATED", 2020, 2026, CREATED_FALLBACK),
    ],
)
def test_fallback_is_year_only_for_match_and_mismatch(
    tmp_path: Path,
    basis: str,
    source_year: int,
    effective_year: int,
    expected_reason: str,
) -> None:
    plan = _build(
        tmp_path / f"{basis}-{effective_year}.csv",
        [_row(_sha("a"), source_year, basis=basis, effective_year=effective_year)],
    )

    assert plan.rows[0].action == SET_YEAR_ONLY
    assert plan.rows[0].reason == expected_reason


def test_cli_writes_plan_in_deterministic_sha_order_and_prints_totals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path = tmp_path / "audit.csv"
    output_path = tmp_path / "plan.csv"
    _write_audit(
        input_path,
        [
            _row(_sha("b"), 2021, basis="IMPORTED", effective_year=2026),
            _row(_sha("a"), 2020, basis="EXIF", effective_year=2013),
        ],
    )

    assert main(["--input", str(input_path), "--output", str(output_path)]) == 0

    with output_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        assert tuple(reader.fieldnames or ()) == PLAN_CSV_FIELDS
        output_rows = list(reader)
    assert [row["sha256"] for row in output_rows] == [_sha("a"), _sha("b")]
    assert [row["current_date_revision"] for row in output_rows] == ["0", "0"]

    output = capsys.readouterr().out
    assert "TO_YEAR_ONLY_TOTAL=2" in output
    assert "AUTO_RECONCILE_WRITE_TOTAL=2" in output
    assert "2020=1" in output
    assert "2021=1" in output
    assert "EXIF_YEAR_CONFLICT=1" in output
    assert "IMPORTED_FALLBACK=1" in output
