from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.models.file_service import CommonFileService


class FileServiceRepository:
    """Create and query service links for shared common file assets."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, *, file_id: int, service_name: str) -> CommonFileService | None:
        return (
            self.db.query(CommonFileService)
            .filter(CommonFileService.file_id == file_id)
            .filter(CommonFileService.service_name == service_name)
            .first()
        )

    def ensure_link(
        self,
        *,
        file_id: int,
        service_name: str,
        commit: bool = True,
    ) -> tuple[CommonFileService, bool]:
        existing = self.get(file_id=file_id, service_name=service_name)
        if existing is not None:
            return existing, False

        link = CommonFileService(file_id=file_id, service_name=service_name)
        self.db.add(link)
        try:
            self.db.flush()
            if commit:
                self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.get(file_id=file_id, service_name=service_name)
            if existing is None:
                raise
            return existing, False
        return link, True
