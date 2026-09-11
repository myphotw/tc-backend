"""Atomic MemoryKeeper user capture-date override mutations."""

from __future__ import annotations

from datetime import datetime, time, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.repositories.change_event_repository import ChangeEventRepository, ChangeOperation
from app.common.repositories.history_repository import HistoryRepository
from app.common.repositories.metadata_priority import MetadataPriority
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.schemas.file import (
    MemoryKeeperCaptureDateUpdateItem,
    MemoryKeeperCaptureDateUpdateRequest,
    MemoryKeeperCaptureDateUpdateResponse,
)
from app.memorykeeper.services.capture_date_service import MemoryKeeperCaptureDateService


class MemoryKeeperCaptureDateOverrideService:
    SERVICE_NAME = "MemoryKeeper"
    RESOURCE_TYPE = "MemoryKeeperCaptureDate"

    def __init__(self, db: Session) -> None:
        self.db = db
        self.history = HistoryRepository(db)
        self.changes = ChangeEventRepository(db)
        self.projection = MemoryKeeperCaptureDateService(db)

    def update(
        self,
        payload: MemoryKeeperCaptureDateUpdateRequest,
    ) -> MemoryKeeperCaptureDateUpdateResponse:
        try:
            rows = (
                self.db.query(CommonFile, CommonFileService)
                .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
                .filter(CommonFile.file_id.in_(payload.file_ids))
                .filter(CommonFile.deleted.is_(False))
                .filter(CommonFileService.service_name == self.SERVICE_NAME)
                .order_by(CommonFile.id.asc())
                .with_for_update(of=CommonFile)
                .all()
            )
            by_public_id = {common_file.file_id: (common_file, link) for common_file, link in rows}
            missing = [file_id for file_id in payload.file_ids if file_id not in by_public_id]
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"code": "MEMORYKEEPER_FILES_NOT_FOUND", "file_ids": missing},
                )

            numeric_ids = [by_public_id[file_id][0].id for file_id in payload.file_ids]
            states = (
                self.db.query(MemoryKeeperFileState)
                .filter(MemoryKeeperFileState.file_id.in_(numeric_ids))
                .order_by(MemoryKeeperFileState.file_id.asc())
                .with_for_update()
                .all()
            )
            states_by_file = {state.file_id: state for state in states}
            metadata_rows = (
                self.db.query(CommonFileMetadata)
                .filter(CommonFileMetadata.file_id.in_(numeric_ids))
                .all()
            )
            metadata_by_file = {metadata.file_id: metadata for metadata in metadata_rows}

            for public_id in payload.file_ids:
                common_file, link = by_public_id[public_id]
                if common_file.id not in states_by_file:
                    states_by_file[common_file.id] = self.projection.synchronize(
                        common_file=common_file,
                        service_link=link,
                        metadata=metadata_by_file.get(common_file.id),
                        state_missing_known=True,
                        initial_favorite=bool(common_file.favorite),
                    )

            conflicts = []
            for public_id in payload.file_ids:
                common_file, _ = by_public_id[public_id]
                current = int(states_by_file[common_file.id].revision or 0)
                expected = payload.expected_date_revisions[public_id]
                if current != expected:
                    conflicts.append(
                        {
                            "file_id": public_id,
                            "expected_revision": expected,
                            "current_revision": current,
                        }
                    )
            if conflicts:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "REVISION_CONFLICT", "files": conflicts},
                )

            requested_datetime = (
                datetime.combine(payload.user_capture_date, time.min)
                if payload.user_capture_date is not None
                else None
            )
            histories: list[dict[str, object]] = []
            updated_states: list[tuple[CommonFile, MemoryKeeperFileState]] = []
            for public_id in payload.file_ids:
                common_file, link = by_public_id[public_id]
                state = states_by_file[common_file.id]
                if state.user_capture_datetime != requested_datetime:
                    histories.append(
                        {
                            "file_id": common_file.id,
                            "field_name": "memorykeeper_user_capture_datetime",
                            "old_value": state.user_capture_datetime,
                            "new_value": requested_datetime,
                            "source": "USER",
                            "priority": MetadataPriority.USER,
                            "modified_by": self.__class__.__name__,
                            "approved": True,
                        }
                    )
                state.user_capture_datetime = requested_datetime
                state.user_capture_precision = "DATE" if requested_datetime is not None else None
                self.projection.synchronize(
                    common_file=common_file,
                    service_link=link,
                    metadata=metadata_by_file.get(common_file.id),
                    state=state,
                )
                state.revision = int(state.revision or 0) + 1
                state.updated_at = datetime.now(timezone.utc)
                self.changes.append(
                    service_name=self.SERVICE_NAME,
                    resource_type=self.RESOURCE_TYPE,
                    resource_id=common_file.file_id,
                    operation=ChangeOperation.UPDATE,
                    revision=state.revision,
                )
                updated_states.append((common_file, state))

            self.history.create_histories(items=histories, commit=False)
            self.db.commit()
            for _, state in updated_states:
                self.db.refresh(state)
            return MemoryKeeperCaptureDateUpdateResponse(
                items=[self._to_item(common_file, state) for common_file, state in updated_states],
                updated_count=len(updated_states),
            )
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _to_item(
        common_file: CommonFile,
        state: MemoryKeeperFileState,
    ) -> MemoryKeeperCaptureDateUpdateItem:
        return MemoryKeeperCaptureDateUpdateItem(
            file_id=common_file.file_id,
            user_capture_datetime=state.user_capture_datetime,
            user_capture_precision=state.user_capture_precision,
            effective_capture_datetime=state.effective_capture_datetime,
            effective_capture_date=state.effective_capture_date,
            effective_capture_year=state.effective_capture_year,
            date_basis=state.date_basis,
            date_revision=int(state.revision or 0),
        )
