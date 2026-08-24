from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


# Stable application-level lock key. Uploads take a shared transaction lock;
# reset execute takes the exclusive form. This adds no table or schema state.
MEMORYKEEPER_RESET_LOCK_KEY = 5569649468952


def acquire_memorykeeper_reset_lock(
    db: Session,
    *,
    exclusive: bool,
) -> None:
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    function = (
        "pg_advisory_xact_lock"
        if exclusive
        else "pg_advisory_xact_lock_shared"
    )
    db.execute(
        text(f"SELECT {function}(:lock_key)"),
        {"lock_key": MEMORYKEEPER_RESET_LOCK_KEY},
    )
