"""로컬 Hash Cache (SQLite)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class FileHashCache:
    """
    PC Watcher 전용 Hash Cache.

    같은 SHA256은 다시 업로드하지 않는다.
    삭제된 파일 기록도 유지한다.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watch_file_cache (
                    hash TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    last_upload TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def has_hash(self, file_hash: str) -> bool:
        """이미 업로드된 Hash인지 확인한다."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM watch_file_cache WHERE hash = ? LIMIT 1",
                (file_hash,),
            ).fetchone()
        return row is not None

    def add(
        self,
        *,
        file_hash: str,
        file_path: str,
        last_upload: datetime | None = None,
    ) -> None:
        """업로드 완료 Hash를 저장한다. 동일 Hash는 최신 경로로 갱신한다."""
        uploaded_at = (last_upload or datetime.now(timezone.utc)).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watch_file_cache (hash, file_path, last_upload)
                VALUES (?, ?, ?)
                ON CONFLICT(hash) DO UPDATE SET
                    file_path = excluded.file_path,
                    last_upload = excluded.last_upload
                """,
                (file_hash, file_path, uploaded_at),
            )
            conn.commit()
