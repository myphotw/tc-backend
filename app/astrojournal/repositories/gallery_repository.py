from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.astrojournal.models.equipment import AstroEquipment
from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.models.plate_solve_job import AstroPlateSolveJob
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService


AstroGalleryRow = tuple[
    ObservationRecord,
    CommonFile,
    CommonFileMetadata | None,
    AstroPlateSolveJob | None,
    AstroEquipment | None,
]


class AstroGalleryRepository:
    SERVICE_NAME = "AstroJournal"

    def __init__(self, db: Session) -> None:
        self.db = db

    def list(
        self,
        *,
        page: int,
        page_size: int,
        catalog_object_id: str | None = None,
        favorite: bool | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> tuple[list[AstroGalleryRow], int]:
        query = self._base_query()
        if catalog_object_id is not None:
            query = query.filter(ObservationRecord.catalog_object_id == catalog_object_id)
        if favorite is not None:
            query = query.filter(ObservationRecord.favorite.is_(favorite))
        if date_from is not None:
            query = query.filter(ObservationRecord.captured_at >= date_from)
        if date_to is not None:
            query = query.filter(ObservationRecord.captured_at <= date_to)

        total = query.count()
        rows = (
            query.order_by(
                ObservationRecord.captured_at.desc(),
                ObservationRecord.created_at.desc(),
                ObservationRecord.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return rows, total

    def get(self, record_id: str) -> AstroGalleryRow | None:
        return self._base_query().filter(ObservationRecord.id == record_id).first()

    def active_record_ids_for_files(self, file_ids: set[int]) -> list[str]:
        if not file_ids:
            return []
        rows = (
            self._base_query()
            .with_entities(ObservationRecord.id)
            .filter(ObservationRecord.file_id.in_(file_ids))
            .distinct()
            .order_by(ObservationRecord.id)
            .all()
        )
        return [row.id for row in rows]

    def current_resource_states(
        self, record_ids: set[str]
    ) -> dict[str, tuple[str, int | None]]:
        """Resolve one change-feed page, including tombstones, in one SELECT.

        Availability follows _base_query's required joins/filters. Metadata,
        equipment and Plate Solve results are optional and do not gate reads.
        """
        if not record_ids:
            return {}
        states = {record_id: ("MISSING", None) for record_id in record_ids}
        rows = (
            self.db.query(
                ObservationRecord.id,
                ObservationRecord.revision,
                ObservationRecord.deleted_at,
                ObservationRecord.service_name,
                CommonFile.id.label("common_file_id"),
                CommonFile.deleted,
                CommonFileService.id.label("service_link_id"),
            )
            .outerjoin(CommonFile, CommonFile.id == ObservationRecord.file_id)
            .outerjoin(
                CommonFileService,
                (CommonFileService.file_id == CommonFile.id)
                & (CommonFileService.service_name == self.SERVICE_NAME),
            )
            .filter(ObservationRecord.id.in_(record_ids))
            .all()
        )
        for row in rows:
            if row.deleted_at is not None:
                state = "DELETED"
            elif (
                row.service_name == self.SERVICE_NAME
                and row.common_file_id is not None
                and row.deleted is False
                and row.service_link_id is not None
            ):
                state = "ACTIVE"
            else:
                state = "UNAVAILABLE"
            states[row.id] = (state, row.revision)
        return states

    def _base_query(self):
        return (
            self.db.query(
                ObservationRecord,
                CommonFile,
                CommonFileMetadata,
                AstroPlateSolveJob,
                AstroEquipment,
            )
            .join(CommonFile, CommonFile.id == ObservationRecord.file_id)
            .join(
                CommonFileService,
                (CommonFileService.file_id == CommonFile.id)
                & (CommonFileService.service_name == self.SERVICE_NAME),
            )
            .outerjoin(CommonFileMetadata, CommonFileMetadata.file_id == CommonFile.id)
            .outerjoin(
                AstroPlateSolveJob,
                AstroPlateSolveJob.common_file_id == CommonFile.id,
            )
            .outerjoin(
                AstroEquipment,
                AstroEquipment.id == ObservationRecord.equipment_ref_id,
            )
            .filter(ObservationRecord.service_name == self.SERVICE_NAME)
            .filter(ObservationRecord.deleted_at.is_(None))
            .filter(CommonFile.deleted.is_(False))
        )
