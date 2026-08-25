from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


ASTROJOURNAL_RESET_LOCK_KEY = 5569650722386


def acquire_astrojournal_reset_lock(
    db: Session,
    *,
    exclusive: bool,
) -> None:
    """Serialize AstroJournal upload creation against Reset on PostgreSQL."""
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
        {"lock_key": ASTROJOURNAL_RESET_LOCK_KEY},
    )
