"""QA scripts shared environment guard.

운영 DATABASE_URL / PHOTO_PLATFORM_ROOT 에서의 성능 테스트 실행을 차단한다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]


def _looks_like_test_name(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    markers = ("test", "qa", "staging-test", "_test", "testdb")
    return any(marker in lowered for marker in markers)


def _apply_test_database_url(test_db: str) -> str:
    """TEST_DATABASE_URL을 app Settings가 읽는 POSTGRES_* 환경변수로 반영한다."""
    parsed = urlparse(test_db)
    if parsed.scheme not in {"postgresql", "postgres", "postgresql+psycopg2"}:
        print(
            f"REFUSED: TEST_DATABASE_URL must be a postgresql URL (got scheme={parsed.scheme!r})",
            file=sys.stderr,
        )
        raise SystemExit(2)

    db_name = unquote((parsed.path or "").lstrip("/").split("/")[0] or "")
    if not db_name:
        print("REFUSED: TEST_DATABASE_URL is missing database name", file=sys.stderr)
        raise SystemExit(2)

    if parsed.hostname:
        os.environ["POSTGRES_HOST"] = parsed.hostname
    if parsed.port:
        os.environ["POSTGRES_PORT"] = str(parsed.port)
    if parsed.username:
        os.environ["POSTGRES_USER"] = unquote(parsed.username)
    if parsed.password is not None:
        os.environ["POSTGRES_PASSWORD"] = unquote(parsed.password)
    os.environ["POSTGRES_DB"] = db_name
    return db_name


def require_test_environment(*, script_name: str) -> None:
    """
    성능/병렬 QA 스크립트 실행 전 환경을 검증한다.

    요구사항:
    - TEST_DATABASE_URL 필수
    - DB 이름 또는 URL에 test/qa 표시
    - PHOTO_PLATFORM_ROOT_TEST 또는 TEST_PHOTO_PLATFORM_ROOT 필수
    - 운영 .env POSTGRES_DB 와 동일하고 test 표시가 없으면 거부
    """
    test_db = (os.environ.get("TEST_DATABASE_URL") or "").strip()
    if not test_db:
        print(
            f"[{script_name}] REFUSED: TEST_DATABASE_URL is required. "
            "Do not run QA perf scripts against the operational database.\n"
            "Example:\n"
            "  set TEST_DATABASE_URL=postgresql://user:pass@host:5432/tc_backend_test\n"
            "  set PHOTO_PLATFORM_ROOT_TEST=D:/tmp/PhotoPlatformTest",
            file=sys.stderr,
        )
        raise SystemExit(2)

    db_name = _apply_test_database_url(test_db)
    if not _looks_like_test_name(db_name) and not _looks_like_test_name(test_db):
        print(
            f"[{script_name}] REFUSED: TEST_DATABASE_URL must include a test/qa marker "
            f"in the database name or URL (got db_name={db_name!r}).",
            file=sys.stderr,
        )
        raise SystemExit(2)

    test_root = (
        os.environ.get("PHOTO_PLATFORM_ROOT_TEST")
        or os.environ.get("TEST_PHOTO_PLATFORM_ROOT")
        or ""
    ).strip()
    if not test_root:
        print(
            f"[{script_name}] REFUSED: PHOTO_PLATFORM_ROOT_TEST "
            "(or TEST_PHOTO_PLATFORM_ROOT) is required for isolated storage.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    os.environ["PHOTO_PLATFORM_ROOT"] = test_root
    Path(test_root).mkdir(parents=True, exist_ok=True)

    try:
        from dotenv import dotenv_values

        env_path = ROOT / ".env"
        if env_path.is_file():
            values = dotenv_values(env_path)
            prod_db = (values.get("POSTGRES_DB") or "").strip()
            if prod_db and prod_db == db_name and not _looks_like_test_name(prod_db):
                print(
                    f"[{script_name}] REFUSED: TEST_DATABASE_URL points to operational "
                    f"POSTGRES_DB={prod_db!r}.",
                    file=sys.stderr,
                )
                raise SystemExit(2)
    except ImportError:
        pass

    print(
        f"[{script_name}] test environment OK "
        f"db_name={db_name} storage_root={test_root}"
    )
