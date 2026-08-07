from __future__ import annotations

from sqlalchemy.orm import Session

from app.common.repositories.change_event_repository import ChangeEventRepository
from app.common.schemas.changes import ChangeEventResponse, ChangesResponse


class ChangesService:
    def __init__(self, db: Session) -> None:
        self.repository = ChangeEventRepository(db)

    def list_changes(
        self,
        *,
        cursor: int = 0,
        limit: int = 100,
        service_name: str | None = None,
    ) -> ChangesResponse:
        rows, has_more = self.repository.list_after(
            cursor=cursor,
            limit=limit,
            service_name=service_name,
        )
        items = [ChangeEventResponse.model_validate(row) for row in rows]
        return ChangesResponse(
            items=items,
            next_cursor=items[-1].cursor if items else cursor,
            has_more=has_more,
        )
