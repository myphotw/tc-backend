from __future__ import annotations

from sqlalchemy.orm import Session

from app.common.models.change_event import CommonChangeEvent


class ChangeOperation:
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


class ChangeEventRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def append(
        self,
        *,
        service_name: str,
        resource_type: str,
        resource_id: str,
        operation: str,
        revision: int | None,
        tombstone: bool = False,
    ) -> CommonChangeEvent:
        event = CommonChangeEvent(
            service_name=service_name,
            resource_type=resource_type,
            resource_id=resource_id,
            operation=operation,
            revision=revision,
            tombstone=tombstone,
        )
        self.db.add(event)
        self.db.flush()
        return event

    def list_after(
        self,
        *,
        cursor: int,
        limit: int,
        service_name: str | None = None,
    ) -> tuple[list[CommonChangeEvent], bool]:
        query = self.db.query(CommonChangeEvent).filter(CommonChangeEvent.id > cursor)
        if service_name is not None:
            query = query.filter(CommonChangeEvent.service_name == service_name)
        rows = query.order_by(CommonChangeEvent.id.asc()).limit(limit + 1).all()
        return rows[:limit], len(rows) > limit
