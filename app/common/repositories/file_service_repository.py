from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
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

    def has_active_file_by_sha(
        self,
        *,
        content_sha256: str,
        service_name: str,
    ) -> bool:
        """Return whether an active FileAsset belongs to one service domain."""

        return (
            self.db.query(CommonFileService.id)
            .join(CommonFile, CommonFile.id == CommonFileService.file_id)
            .filter(CommonFile.file_id == content_sha256)
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == service_name)
            .first()
            is not None
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

        # begin_nested() flushes pending caller changes unconditionally. Keep
        # that flush outside the uniqueness handler: unrelated caller failures
        # must not be mistaken for a duplicate service link.
        self.db.flush()
        link = CommonFileService(file_id=file_id, service_name=service_name)
        created = True
        try:
            with self.db.begin_nested():
                self.db.add(link)
                self.db.flush()
        except IntegrityError:
            if not self.db.is_active:
                raise
            existing = self.get(file_id=file_id, service_name=service_name)
            if existing is None:
                raise
            link = existing
            created = False
        if commit:
            self.db.commit()
        return link, created
