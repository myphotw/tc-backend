from __future__ import annotations

import csv
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.common.model_registry import Base
from app.common.models.change_event import CommonChangeEvent
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.metadata_history import CommonMetadataHistory
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from scripts.plan_memorykeeper_source_year_reconciliation import (
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
from scripts.reconcile_memorykeeper_source_years import (
    ALREADY_APPLIED,
    HISTORY_SOURCE,
    KEEP_USER,
    MISSING_FILE,
    MISSING_MEMORYKEEPER_LINK,
    MISSING_STATE,
    OTHER_CONFLICT,
    PROJECTION_CONFLICT,
    READY_PROVENANCE_ONLY_EXIF,
    READY_YEAR_ONLY_CREATED,
    READY_YEAR_ONLY_EXIF_CONFLICT,
    READY_YEAR_ONLY_IMPORTED,
    REPORT_CSV_FIELDS,
    SNAPSHOT_MISMATCH,
    SOURCE_BASIS,
    STALE_REVISION,
    USER_CONFLICT_REVIEW,
    MemoryKeeperSourceYearReconciler,
    ReconciliationApplyError,
    ReconciliationPlanRow,
    ReconciliationValidationError,
    load_plan_csv,
    main,
    print_summary,
    write_report_csv,
)


def _sha(character: str) -> str:
    return character * 64


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(
    db: Session,
    character: str,
    *,
    basis: str = "EXIF",
    effective_datetime: datetime = datetime(2020, 6, 15, 12, 30),
    original_datetime: datetime | None = datetime(2020, 6, 15, 12, 30),
    user_datetime: datetime | None = None,
    user_precision: str | None = None,
    revision: int = 3,
    deleted: bool = False,
    with_link: bool = True,
    with_state: bool = True,
) -> tuple[CommonFile, MemoryKeeperFileState | None]:
    item = CommonFile(
        file_id=_sha(character),
        original_name=f"{character}.jpg",
        service_name="MemoryKeeper",
        created_at=datetime(2018, 1, 2, 3, 4),
        deleted=deleted,
    )
    db.add(item)
    db.flush()
    if with_link:
        db.add(
            CommonFileService(
                file_id=item.id,
                service_name="MemoryKeeper",
                created_at=datetime(2019, 2, 3, 4, 5),
            )
        )
    if original_datetime is not None:
        db.add(
            CommonFileMetadata(
                file_id=item.id,
                original_capture_datetime=original_datetime,
            )
        )
    state = None
    if with_state:
        state = MemoryKeeperFileState(
            file_id=item.id,
            favorite=False,
            revision=revision,
            user_capture_datetime=user_datetime,
            user_capture_precision=user_precision,
            effective_capture_datetime=effective_datetime,
            effective_capture_date=effective_datetime.date(),
            legacy_effective_capture_year=effective_datetime.year,
            effective_capture_year=effective_datetime.year,
            effective_capture_precision=(user_precision or "DATETIME"),
            date_basis=basis,
        )
        db.add(state)
    db.commit()
    return item, state


def _plan(
    item: CommonFile,
    state: MemoryKeeperFileState,
    *,
    source_year: int,
    action: str,
    reason: str,
    review_reason: str | None = None,
    row_number: int = 2,
) -> ReconciliationPlanRow:
    return ReconciliationPlanRow(
        row_number=row_number,
        sha256=item.file_id,
        source_year=source_year,
        action=action,
        reason=reason,
        review_reason=review_reason,
        current_date_basis=state.date_basis,
        current_effective_year=state.effective_capture_year,
        current_effective_date=state.effective_capture_date,
        current_effective_datetime=state.effective_capture_datetime,
        current_user_datetime=state.user_capture_datetime,
        current_user_precision=state.user_capture_precision,
        current_revision=int(state.revision or 0),
        source_path_count=1,
    )


def _csv_row(plan: ReconciliationPlanRow) -> dict[str, object]:
    return {
        "sha256": plan.sha256 or "",
        "original_path_year": plan.source_year or "",
        "action": plan.action,
        "reason": plan.reason,
        "review_reason": plan.review_reason or "",
        "current_date_basis": plan.current_date_basis or "",
        "current_effective_capture_year": plan.current_effective_year or "",
        "current_effective_capture_date": (
            plan.current_effective_date.isoformat()
            if plan.current_effective_date is not None
            else ""
        ),
        "current_effective_capture_datetime": (
            plan.current_effective_datetime.isoformat()
            if plan.current_effective_datetime is not None
            else ""
        ),
        "current_user_capture_datetime": (
            plan.current_user_datetime.isoformat()
            if plan.current_user_datetime is not None
            else ""
        ),
        "current_user_capture_precision": plan.current_user_precision or "",
        "current_date_revision": (
            plan.current_revision if plan.current_revision is not None else ""
        ),
        "source_path_count": plan.source_path_count,
    }


def _write_plan(path: Path, plans: list[ReconciliationPlanRow]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=PLAN_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(_csv_row(plan) for plan in plans)


def _verify_one(
    db: Session,
    plan: ReconciliationPlanRow,
):
    return MemoryKeeperSourceYearReconciler(db).verify_all([plan])[0]


def test_cli_is_dry_run_by_default_and_apply_is_explicit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "reconcile.sqlite"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    seed_db = factory()
    item, state = _seed(seed_db, "a")
    plan_path = tmp_path / "plan.csv"
    _write_plan(
        plan_path,
        [
            _plan(
                item,
                state,
                source_year=2020,
                action=KEEP_EXACT_EXIF,
                reason=EXIF_SOURCE_YEAR_MATCH,
            )
        ],
    )
    seed_db.close()

    with patch("app.common.database.SessionLocal", factory):
        assert main(["--plan", str(plan_path)]) == 0
    check = factory()
    assert check.get(MemoryKeeperFileState, item.id).source_capture_year is None
    assert "mode=dry-run" in capsys.readouterr().out
    check.close()

    with patch("app.common.database.SessionLocal", factory):
        assert main(["--plan", str(plan_path), "--apply"]) == 0
    check = factory()
    assert check.get(MemoryKeeperFileState, item.id).source_capture_year == 2020
    assert "mode=apply" in capsys.readouterr().out
    check.close()
    engine.dispose()


def test_keep_exact_exif_applies_only_provenance_and_audit_contract(
    db: Session,
) -> None:
    item, state = _seed(db, "a")
    plan = _plan(
        item,
        state,
        source_year=2020,
        action=KEEP_EXACT_EXIF,
        reason=EXIF_SOURCE_YEAR_MATCH,
    )
    original = db.query(CommonFileMetadata).filter_by(file_id=item.id).one()
    original_before = original.original_capture_datetime
    user_before = (state.user_capture_datetime, state.user_capture_precision)
    result = _verify_one(db, plan)

    assert result.verification_status == READY_PROVENANCE_ONLY_EXIF
    stats = MemoryKeeperSourceYearReconciler(db).apply_verified([result])

    db.refresh(state)
    assert stats.applied == 1
    assert state.source_capture_year == 2020
    assert state.source_capture_year_basis == SOURCE_BASIS
    assert state.effective_capture_datetime == datetime(2020, 6, 15, 12, 30)
    assert state.effective_capture_date == date(2020, 6, 15)
    assert state.effective_capture_year == 2020
    assert state.effective_capture_precision == "DATETIME"
    assert state.date_basis == "EXIF"
    assert state.revision == 4
    assert (state.user_capture_datetime, state.user_capture_precision) == user_before
    assert original.original_capture_datetime == original_before
    histories = db.query(CommonMetadataHistory).order_by(CommonMetadataHistory.id).all()
    assert [history.field_name for history in histories] == [
        "memorykeeper_source_capture_year",
        "memorykeeper_source_capture_year_basis",
    ]
    assert {history.source for history in histories} == {HISTORY_SOURCE}
    events = db.query(CommonChangeEvent).all()
    assert len(events) == 1
    assert events[0].service_name == "MemoryKeeper"
    assert events[0].resource_type == "MemoryKeeperCaptureDate"
    assert events[0].resource_id == item.file_id
    assert events[0].operation == "UPDATE"
    assert events[0].revision == 4


@pytest.mark.parametrize(
    ("character", "basis", "current_year", "source_year", "reason", "status"),
    [
        ("b", "EXIF", 2013, 2020, EXIF_YEAR_CONFLICT, READY_YEAR_ONLY_EXIF_CONFLICT),
        ("c", "IMPORTED", 2020, 2020, IMPORTED_FALLBACK, READY_YEAR_ONLY_IMPORTED),
        ("d", "IMPORTED", 2026, 2020, IMPORTED_FALLBACK, READY_YEAR_ONLY_IMPORTED),
        ("e", "CREATED", 2026, 2021, CREATED_FALLBACK, READY_YEAR_ONLY_CREATED),
    ],
)
def test_year_only_actions_use_central_projection(
    db: Session,
    character: str,
    basis: str,
    current_year: int,
    source_year: int,
    reason: str,
    status: str,
) -> None:
    current = datetime(current_year, 6, 15, 12, 30)
    original = current if basis == "EXIF" else None
    item, state = _seed(
        db,
        character,
        basis=basis,
        effective_datetime=current,
        original_datetime=original,
    )
    plan = _plan(
        item,
        state,
        source_year=source_year,
        action=SET_YEAR_ONLY,
        reason=reason,
    )
    result = _verify_one(db, plan)

    assert result.verification_status == status
    MemoryKeeperSourceYearReconciler(db).apply_verified([result])

    db.refresh(state)
    assert state.source_capture_year == source_year
    assert state.source_capture_year_basis == SOURCE_BASIS
    assert state.effective_capture_datetime is None
    assert state.effective_capture_date is None
    assert state.effective_capture_year == source_year
    assert state.effective_capture_precision == "YEAR"
    assert state.date_basis == "SOURCE_YEAR"


@pytest.mark.parametrize(
    ("source_year", "review_reason", "expected"),
    [
        (2024, None, KEEP_USER),
        (2020, USER_SOURCE_YEAR_CONFLICT, USER_CONFLICT_REVIEW),
    ],
)
def test_user_rows_are_never_written(
    db: Session,
    source_year: int,
    review_reason: str | None,
    expected: str,
) -> None:
    item, state = _seed(
        db,
        "f",
        basis="USER",
        effective_datetime=datetime(2024, 5, 6),
        user_datetime=datetime(2024, 5, 6),
        user_precision="DATE",
    )
    plan = _plan(
        item,
        state,
        source_year=source_year,
        action=KEEP_EXACT_USER,
        reason=USER_EXACT,
        review_reason=review_reason,
    )
    result = _verify_one(db, plan)

    assert result.verification_status == expected
    stats = MemoryKeeperSourceYearReconciler(db).apply_verified([result])
    db.refresh(state)
    assert stats.applied == 0
    assert state.source_capture_year is None
    assert state.user_capture_datetime == datetime(2024, 5, 6)
    assert state.revision == 3
    assert db.query(CommonMetadataHistory).count() == 0
    assert db.query(CommonChangeEvent).count() == 0


def test_user_added_after_plan_is_protected_even_without_revision_change(
    db: Session,
) -> None:
    item, state = _seed(db, "1")
    plan = _plan(
        item,
        state,
        source_year=2020,
        action=KEEP_EXACT_EXIF,
        reason=EXIF_SOURCE_YEAR_MATCH,
    )
    state.user_capture_datetime = datetime(2025, 1, 2)
    state.user_capture_precision = "DATE"
    state.effective_capture_datetime = datetime(2025, 1, 2)
    state.effective_capture_date = date(2025, 1, 2)
    state.effective_capture_year = 2025
    state.date_basis = "USER"
    db.commit()

    result = _verify_one(db, plan)
    assert result.verification_status == USER_CONFLICT_REVIEW
    assert MemoryKeeperSourceYearReconciler(db).apply_verified([result]).applied == 0
    assert state.user_capture_datetime == datetime(2025, 1, 2)
    assert state.source_capture_year is None


def test_revision_and_snapshot_changes_fail_closed(db: Session) -> None:
    revision_item, revision_state = _seed(db, "2")
    revision_plan = _plan(
        revision_item,
        revision_state,
        source_year=2020,
        action=KEEP_EXACT_EXIF,
        reason=EXIF_SOURCE_YEAR_MATCH,
    )
    revision_state.revision += 1
    db.commit()
    assert _verify_one(db, revision_plan).verification_status == STALE_REVISION

    snapshot_item, snapshot_state = _seed(db, "3")
    snapshot_plan = _plan(
        snapshot_item,
        snapshot_state,
        source_year=2020,
        action=KEEP_EXACT_EXIF,
        reason=EXIF_SOURCE_YEAR_MATCH,
    )
    snapshot_state.effective_capture_datetime = datetime(2020, 7, 1)
    snapshot_state.effective_capture_date = date(2020, 7, 1)
    db.commit()
    assert _verify_one(db, snapshot_plan).verification_status == SNAPSHOT_MISMATCH

    with pytest.raises(ReconciliationApplyError, match="blocking"):
        MemoryKeeperSourceYearReconciler(db).apply_verified(
            [
                _verify_one(db, revision_plan),
                _verify_one(db, snapshot_plan),
            ]
        )
    assert revision_state.source_capture_year is None
    assert snapshot_state.source_capture_year is None


def test_locked_reverification_rejects_a_revision_changed_after_dry_run(
    db: Session,
) -> None:
    item, state = _seed(db, "4")
    plan = _plan(
        item,
        state,
        source_year=2020,
        action=KEEP_EXACT_EXIF,
        reason=EXIF_SOURCE_YEAR_MATCH,
    )
    reconciler = MemoryKeeperSourceYearReconciler(db)
    verified = reconciler.verify_all([plan])
    assert verified[0].verification_status == READY_PROVENANCE_ONLY_EXIF
    state.revision += 1
    db.commit()

    with pytest.raises(ReconciliationApplyError, match="STALE_REVISION"):
        reconciler.apply_verified(verified)

    db.refresh(state)
    assert state.source_capture_year is None
    assert db.query(CommonMetadataHistory).count() == 0
    assert db.query(CommonChangeEvent).count() == 0


def test_missing_deleted_link_and_state_are_classified(db: Session) -> None:
    deleted_item, deleted_state = _seed(db, "5", deleted=True)
    no_link_item, no_link_state = _seed(db, "6", with_link=False)
    no_state_item, _ = _seed(db, "7", with_state=False)
    missing_state_snapshot = MemoryKeeperFileState(
        file_id=no_state_item.id,
        revision=0,
        effective_capture_datetime=datetime(2020, 6, 15, 12, 30),
        effective_capture_date=date(2020, 6, 15),
        effective_capture_year=2020,
        effective_capture_precision="DATETIME",
        date_basis="EXIF",
    )
    missing_plan = ReconciliationPlanRow(
        row_number=5,
        sha256=_sha("8"),
        source_year=2020,
        action=KEEP_EXACT_EXIF,
        reason=EXIF_SOURCE_YEAR_MATCH,
        review_reason=None,
        current_date_basis="EXIF",
        current_effective_year=2020,
        current_effective_date=date(2020, 6, 15),
        current_effective_datetime=datetime(2020, 6, 15, 12, 30),
        current_user_datetime=None,
        current_user_precision=None,
        current_revision=0,
        source_path_count=1,
    )
    plans = [
        _plan(
            deleted_item,
            deleted_state,
            source_year=2020,
            action=KEEP_EXACT_EXIF,
            reason=EXIF_SOURCE_YEAR_MATCH,
        ),
        _plan(
            no_link_item,
            no_link_state,
            source_year=2020,
            action=KEEP_EXACT_EXIF,
            reason=EXIF_SOURCE_YEAR_MATCH,
        ),
        _plan(
            no_state_item,
            missing_state_snapshot,
            source_year=2020,
            action=KEEP_EXACT_EXIF,
            reason=EXIF_SOURCE_YEAR_MATCH,
        ),
        missing_plan,
    ]

    statuses = {
        result.plan.sha256: result.verification_status
        for result in MemoryKeeperSourceYearReconciler(db).verify_all(plans)
    }
    assert statuses[deleted_item.file_id] == MISSING_FILE
    assert statuses[no_link_item.file_id] == MISSING_MEMORYKEEPER_LINK
    assert statuses[no_state_item.file_id] == MISSING_STATE
    assert statuses[_sha("8")] == MISSING_FILE


def test_idempotent_exact_and_year_only_and_partial_conflict(db: Session) -> None:
    exact_item, exact_state = _seed(db, "8")
    exact_state.source_capture_year = 2020
    exact_state.source_capture_year_basis = SOURCE_BASIS
    db.commit()
    exact_plan = _plan(
        exact_item,
        exact_state,
        source_year=2020,
        action=KEEP_EXACT_EXIF,
        reason=EXIF_SOURCE_YEAR_MATCH,
    )
    assert _verify_one(db, exact_plan).verification_status == ALREADY_APPLIED

    year_item, year_state = _seed(
        db,
        "9",
        basis="EXIF",
        effective_datetime=datetime(2013, 6, 15, 12, 30),
        original_datetime=datetime(2013, 6, 15, 12, 30),
    )
    year_plan = _plan(
        year_item,
        year_state,
        source_year=2020,
        action=SET_YEAR_ONLY,
        reason=EXIF_YEAR_CONFLICT,
    )
    year_state.source_capture_year = 2020
    year_state.source_capture_year_basis = SOURCE_BASIS
    year_state.effective_capture_datetime = None
    year_state.effective_capture_date = None
    year_state.effective_capture_year = 2020
    year_state.effective_capture_precision = "YEAR"
    year_state.date_basis = "SOURCE_YEAR"
    db.commit()
    assert _verify_one(db, year_plan).verification_status == ALREADY_APPLIED

    partial_item, partial_state = _seed(db, "0")
    partial_plan = _plan(
        partial_item,
        partial_state,
        source_year=2020,
        action=KEEP_EXACT_EXIF,
        reason=EXIF_SOURCE_YEAR_MATCH,
    )
    partial_state.source_capture_year = 2020
    partial_state.source_capture_year_basis = SOURCE_BASIS
    partial_state.effective_capture_datetime = datetime(2019, 1, 1)
    partial_state.effective_capture_date = date(2019, 1, 1)
    partial_state.effective_capture_year = 2019
    db.commit()
    assert _verify_one(db, partial_plan).verification_status == PROJECTION_CONFLICT

    other_item, other_state = _seed(db, "a")
    other_plan = _plan(
        other_item,
        other_state,
        source_year=2020,
        action=KEEP_EXACT_EXIF,
        reason=EXIF_SOURCE_YEAR_MATCH,
    )
    other_state.source_capture_year = 2019
    other_state.source_capture_year_basis = SOURCE_BASIS
    db.commit()
    assert _verify_one(db, other_plan).verification_status == OTHER_CONFLICT


def test_batch_is_atomic_and_prior_batches_are_retryable(db: Session) -> None:
    rows = []
    states = []
    for character in ("a", "b", "c"):
        item, state = _seed(db, character)
        states.append(state)
        rows.append(
            _plan(
                item,
                state,
                source_year=2020,
                action=KEEP_EXACT_EXIF,
                reason=EXIF_SOURCE_YEAR_MATCH,
            )
        )
    reconciler = MemoryKeeperSourceYearReconciler(db)
    results = reconciler.verify_all(rows)
    original_apply = reconciler._apply_context

    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("batch failure")
        return original_apply(*args, **kwargs)

    with patch.object(reconciler, "_apply_context", side_effect=fail_second):
        with pytest.raises(RuntimeError, match="batch failure"):
            reconciler.apply_verified(results, batch_size=2)
    for state in states:
        db.refresh(state)
        assert state.source_capture_year is None
    assert db.query(CommonMetadataHistory).count() == 0
    assert db.query(CommonChangeEvent).count() == 0

    calls = 0

    def fail_third(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("later batch failure")
        return original_apply(*args, **kwargs)

    with patch.object(reconciler, "_apply_context", side_effect=fail_third):
        with pytest.raises(RuntimeError, match="later batch failure"):
            reconciler.apply_verified(results, batch_size=2)
    db.expire_all()
    assert db.get(MemoryKeeperFileState, states[0].file_id).source_capture_year == 2020
    assert db.get(MemoryKeeperFileState, states[1].file_id).source_capture_year == 2020
    assert db.get(MemoryKeeperFileState, states[2].file_id).source_capture_year is None

    rerun = MemoryKeeperSourceYearReconciler(db).apply_verified(results, batch_size=2)
    assert rerun.applied == 1
    assert rerun.already_applied_during_lock == 2
    assert db.get(MemoryKeeperFileState, states[2].file_id).source_capture_year == 2020
    assert db.query(CommonMetadataHistory).count() == 6
    assert db.query(CommonChangeEvent).count() == 3


def test_report_and_summary_are_deterministic(db: Session, tmp_path: Path) -> None:
    first, first_state = _seed(db, "b")
    second, second_state = _seed(
        db,
        "a",
        basis="IMPORTED",
        original_datetime=None,
    )
    plans = [
        _plan(
            first,
            first_state,
            source_year=2020,
            action=KEEP_EXACT_EXIF,
            reason=EXIF_SOURCE_YEAR_MATCH,
        ),
        _plan(
            second,
            second_state,
            source_year=2019,
            action=SET_YEAR_ONLY,
            reason=IMPORTED_FALLBACK,
        ),
    ]
    results = MemoryKeeperSourceYearReconciler(db).verify_all(plans)
    stream = StringIO()
    print_summary(results, stream)
    assert "READY_WRITE_TOTAL=2" in stream.getvalue()
    assert "DB_VERIFIED_YEAR_ONLY_TOTAL=1" in stream.getvalue()

    report = tmp_path / "report.csv"
    write_report_csv(results, report)
    with report.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        rows = list(reader)
    assert tuple(reader.fieldnames or ()) == REPORT_CSV_FIELDS
    assert [row["sha256"] for row in rows] == [_sha("a"), _sha("b")]
    with pytest.raises(FileExistsError):
        write_report_csv(results, report)


def test_plan_csv_validation_is_fail_closed(tmp_path: Path) -> None:
    valid = ReconciliationPlanRow(
        row_number=2,
        sha256=_sha("a"),
        source_year=2020,
        action=KEEP_EXACT_EXIF,
        reason=EXIF_SOURCE_YEAR_MATCH,
        review_reason=None,
        current_date_basis="EXIF",
        current_effective_year=2020,
        current_effective_date=date(2020, 6, 15),
        current_effective_datetime=datetime(2020, 6, 15, 12, 30),
        current_user_datetime=None,
        current_user_precision=None,
        current_revision=3,
        source_path_count=1,
    )
    duplicate_path = tmp_path / "duplicate.csv"
    _write_plan(duplicate_path, [valid, valid])
    with pytest.raises(ReconciliationValidationError, match="duplicate sha256"):
        load_plan_csv(duplicate_path)

    invalid_path = tmp_path / "invalid.csv"
    invalid = _csv_row(valid)
    invalid["action"] = SET_YEAR_ONLY
    invalid["reason"] = CREATED_FALLBACK
    with invalid_path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=PLAN_CSV_FIELDS)
        writer.writeheader()
        writer.writerow(invalid)
    with pytest.raises(ReconciliationValidationError, match="current_date_basis"):
        load_plan_csv(invalid_path)

    excluded_path = tmp_path / "excluded.csv"
    excluded = {field: "" for field in PLAN_CSV_FIELDS}
    excluded.update({"action": EXCLUDE, "reason": "NO_PATH_YEAR"})
    with excluded_path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=PLAN_CSV_FIELDS)
        writer.writeheader()
        writer.writerow(excluded)
    assert load_plan_csv(excluded_path)[0].action == EXCLUDE
