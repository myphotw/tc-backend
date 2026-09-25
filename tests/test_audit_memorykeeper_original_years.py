from __future__ import annotations

import csv
from datetime import date, datetime
import hashlib
import io
from pathlib import Path

from scripts.audit_memorykeeper_original_years import (
    AMBIGUOUS_SOURCE_YEAR,
    HASH_ERROR,
    MATCH,
    MISSING_EFFECTIVE_YEAR,
    NOT_IN_MEMORYKEEPER,
    NO_PATH_YEAR,
    YEAR_MISMATCH,
    AuditRow,
    MemoryKeeperAuditRecord,
    audit_sources,
    build_parser,
    iter_source_files,
    original_path_year,
    sha256_file,
    write_csv,
)


def _write(root: Path, relative_path: str, content: bytes) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_path_year_is_strict_and_synology_directories_are_excluded(
    tmp_path: Path,
) -> None:
    valid = _write(tmp_path, "2018/태안/사진.jpg", b"valid")
    root_file = _write(tmp_path, "VID_20260105_112259_001.mp4", b"root")
    non_year = _write(tmp_path, "여행2024/사진.jpg", b"non-year")
    _write(tmp_path, "2018/@eaDir/hidden.jpg", b"hidden")
    _write(tmp_path, "#recycle/deleted.jpg", b"deleted")

    files = list(iter_source_files(tmp_path))

    assert valid in files
    assert root_file in files
    assert non_year in files
    assert not any("@eaDir" in path.parts for path in files)
    assert not any("#recycle" in path.parts for path in files)
    assert original_path_year(tmp_path, valid) == 2018
    assert original_path_year(tmp_path, root_file) is None
    assert original_path_year(tmp_path, non_year) is None


def test_sha256_file_streams_the_expected_digest(tmp_path: Path) -> None:
    content = (b"streaming-sha256" * 1000) + b"tail"
    path = _write(tmp_path, "2024/file.bin", content)

    digest, bytes_read = sha256_file(path, chunk_size=17)

    assert digest == hashlib.sha256(content).hexdigest()
    assert bytes_read == len(content)


def test_audit_classifies_all_year_and_duplicate_cases(tmp_path: Path) -> None:
    contents = {
        "match": b"match",
        "mismatch": b"mismatch",
        "missing": b"missing",
        "not_in": b"not-in",
        "same_year_duplicate": b"same-year-duplicate",
        "ambiguous": b"ambiguous",
    }
    _write(tmp_path, "2018/A/match.jpg", contents["match"])
    _write(tmp_path, "2018/A/mismatch.jpg", contents["mismatch"])
    _write(tmp_path, "2019/A/missing.jpg", contents["missing"])
    _write(tmp_path, "2020/A/not-in.jpg", contents["not_in"])
    _write(tmp_path, "2021/A/duplicate.jpg", contents["same_year_duplicate"])
    _write(tmp_path, "2021/B/duplicate-copy.jpg", contents["same_year_duplicate"])
    _write(tmp_path, "2018/C/ambiguous.jpg", contents["ambiguous"])
    _write(tmp_path, "2020/C/ambiguous-copy.jpg", contents["ambiguous"])

    records = {
        _digest(contents["match"]): MemoryKeeperAuditRecord(
            effective_capture_year=2018,
            effective_capture_date=date(2018, 1, 2),
            effective_capture_datetime=datetime(2018, 1, 2, 3, 4, 5),
            date_basis="EXIF",
            memorykeeper_place_id="place-1",
            place_name="태안",
            date_revision=2,
        ),
        _digest(contents["mismatch"]): MemoryKeeperAuditRecord(
            effective_capture_year=2017,
            date_basis="USER",
        ),
        _digest(contents["missing"]): MemoryKeeperAuditRecord(),
        _digest(contents["same_year_duplicate"]): MemoryKeeperAuditRecord(
            effective_capture_year=2021,
        ),
        _digest(contents["ambiguous"]): MemoryKeeperAuditRecord(
            effective_capture_year=2018,
        ),
    }

    rows, _, _ = audit_sources(
        root=tmp_path,
        memorykeeper_records=records,
        progress_every=100,
        progress_stream=io.StringIO(),
    )
    by_name = {row.original_filename: row for row in rows}

    assert by_name["match.jpg"].status == MATCH
    assert by_name["match.jpg"].place_name == "태안"
    assert by_name["mismatch.jpg"].status == YEAR_MISMATCH
    assert by_name["missing.jpg"].status == MISSING_EFFECTIVE_YEAR
    assert by_name["not-in.jpg"].status == NOT_IN_MEMORYKEEPER
    assert by_name["duplicate.jpg"].status == MATCH
    assert by_name["duplicate.jpg"].source_duplicate_count == 2
    assert by_name["duplicate-copy.jpg"].status == MATCH
    assert by_name["ambiguous.jpg"].status == AMBIGUOUS_SOURCE_YEAR
    assert by_name["ambiguous-copy.jpg"].status == AMBIGUOUS_SOURCE_YEAR
    assert by_name["ambiguous.jpg"].source_duplicate_count == 2


def test_no_path_year_is_reported_without_hashing(tmp_path: Path) -> None:
    root_file = _write(tmp_path, "VID_20260105_112259_001.mp4", b"video")
    hashed_paths: list[Path] = []

    def track_hash(path: Path) -> tuple[str, int]:
        hashed_paths.append(path)
        return sha256_file(path)

    rows, progress, _ = audit_sources(
        root=tmp_path,
        memorykeeper_records={},
        hash_file=track_hash,
        progress_stream=io.StringIO(),
    )

    assert [row.status for row in rows] == [NO_PATH_YEAR]
    assert rows[0].original_path == str(root_file)
    assert rows[0].sha256 is None
    assert hashed_paths == []
    assert progress.hashed == 0


def test_max_files_limits_year_candidates_without_counting_no_path_year(
    tmp_path: Path,
) -> None:
    root_file = _write(tmp_path, "000-root-file.jpg", b"root")
    first = _write(tmp_path, "2018/01-first.jpg", b"first")
    second = _write(tmp_path, "2018/02-second.jpg", b"second")
    _write(tmp_path, "2018/03-not-scanned.jpg", b"third")
    _write(tmp_path, "2019/01-not-scanned.jpg", b"fourth")
    hashed_paths: list[Path] = []

    def track_hash(path: Path) -> tuple[str, int]:
        hashed_paths.append(path)
        return sha256_file(path)

    rows, progress, _ = audit_sources(
        root=tmp_path,
        memorykeeper_records={},
        max_files=2,
        hash_file=track_hash,
        progress_stream=io.StringIO(),
    )

    assert hashed_paths == [first, second]
    assert [row.original_path for row in rows] == [
        str(root_file),
        str(first),
        str(second),
    ]
    assert rows[0].status == NO_PATH_YEAR
    assert progress.scanned == 3
    assert progress.hashed == 2


def test_max_files_cli_requires_a_positive_integer() -> None:
    parser = build_parser()

    assert parser.parse_args(["--output", "result.csv", "--max-files", "20"]).max_files == 20
    for invalid in ("0", "-1", "not-a-number"):
        try:
            parser.parse_args(["--output", "result.csv", "--max-files", invalid])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"--max-files accepted invalid value: {invalid}")


def test_hash_error_does_not_stop_remaining_files(tmp_path: Path) -> None:
    failed = _write(tmp_path, "2018/failed.jpg", b"failed")
    healthy = _write(tmp_path, "2018/healthy.jpg", b"healthy")

    def selective_hash(path: Path) -> tuple[str, int]:
        if path == failed:
            raise PermissionError("denied")
        return sha256_file(path)

    rows, progress, _ = audit_sources(
        root=tmp_path,
        memorykeeper_records={},
        hash_file=selective_hash,
        progress_stream=io.StringIO(),
    )
    by_name = {row.original_filename: row for row in rows}

    assert by_name["failed.jpg"].status == HASH_ERROR
    assert "PermissionError" in (by_name["failed.jpg"].error or "")
    assert by_name["healthy.jpg"].status == NOT_IN_MEMORYKEEPER
    assert progress.hash_errors == 1
    assert progress.hashed == 1


def test_csv_is_utf8_bom_and_preserves_korean_paths(tmp_path: Path) -> None:
    output = tmp_path / "결과.csv"
    row = AuditRow(
        scan_index=1,
        status=MATCH,
        original_path_year=2024,
        effective_capture_year=2024,
        sha256="a" * 64,
        original_path="/volume1/여행/여행사진/2024/제주/사진.jpg",
        original_filename="사진.jpg",
        place_name="제주",
    )

    write_csv([row], output)

    raw = output.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with output.open(encoding="utf-8-sig", newline="") as source:
        parsed = list(csv.DictReader(source))
    assert parsed[0]["original_filename"] == "사진.jpg"
    assert parsed[0]["place_name"] == "제주"
