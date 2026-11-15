from sqlalchemy.orm import Session

from app.astrojournal.repositories.gallery_repository import AstroGalleryRepository
from app.common.repositories.change_event_repository import ChangeEventRepository, ChangeOperation


class AstroGalleryVisibilityService:
    """Append projection invalidations in the caller's recovery transaction."""

    RESOURCE_TYPE = "AstroGalleryVisibility"

    def __init__(self, db: Session) -> None:
        self.gallery = AstroGalleryRepository(db)
        self.changes = ChangeEventRepository(db)

    def notify_for_files(self, file_ids: list[int]) -> list[str]:
        record_ids = self.gallery.active_record_ids_for_files(set(file_ids))
        for record_id in record_ids:
            self.changes.append(
                service_name="AstroJournal",
                resource_type=self.RESOURCE_TYPE,
                resource_id=record_id,
                operation=ChangeOperation.UPDATE,
                revision=None,
            )
        return record_ids
