"""PostgreSQL commit-gap fence for the existing integer cursor feed.

Writers share a transaction lock before allocating event IDs. A read-only feed
transaction tries the exclusive lock before its READ COMMITTED snapshot query.
Thus IDs may commit out of order, but no consumer can advance past an in-flight
lower ID. Shared writers never wait for one another while holding domain rows.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi import HTTPException


# ASCII TCCHGFED; distinct from Reset, migration, SHA and integration-suite keys.
CHANGE_FEED_FENCE_KEY = 0x5443434847464544
_WRITER_TRANSACTION = "tc_backend_change_feed_writer_transaction"


def acquire_change_feed_write_fence(db: Session) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    with db.no_autoflush:
        db.execute(
            text("SELECT pg_advisory_xact_lock_shared(:key)"),
            {"key": CHANGE_FEED_FENCE_KEY},
        )
    # Do not cache lock acquisition: a savepoint rollback can release a lock
    # acquired inside it, even though the outer transaction still exists.
    db.info[_WRITER_TRANSACTION] = db.get_transaction()


def acquire_change_feed_read_fence(db: Session) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    transaction = db.get_transaction()
    if (
        transaction is not None
        and db.info.get(_WRITER_TRANSACTION) is transaction
    ) or db.new or db.dirty or db.deleted:
        raise RuntimeError("Read the change feed in an independent read-only transaction")
    with db.no_autoflush:
        if db.connection().get_isolation_level() != "READ COMMITTED":
            raise RuntimeError("The change feed requires READ COMMITTED isolation")
        # Never queue an exclusive reader behind writers. A queued reader could
        # block a second shared writer that holds a row needed by the first
        # writer. Fail/retry the read instead; no cursor is returned or advanced.
        acquired = db.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": CHANGE_FEED_FENCE_KEY},
        ).scalar_one()
        if not acquired:
            raise HTTPException(
                status_code=503,
                detail={"code": "CHANGE_FEED_BUSY"},
                headers={"Retry-After": "1"},
            )
