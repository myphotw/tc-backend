"""Atomic MemoryKeeper photo-category mutations."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.repositories.change_event_repository import (
    ChangeEventRepository,
    ChangeOperation,
)
from app.common.repositories.history_repository import HistoryRepository
from app.common.repositories.metadata_priority import MetadataPriority
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.schemas.file import (
    MemoryKeeperPhotoCategoryUpdateItem,
    MemoryKeeperPhotoCategoryUpdateRequest,
    MemoryKeeperPhotoCategoryUpdateResponse,
)
from app.memorykeeper.services.capture_date_service import MemoryKeeperCaptureDateService
from app.memorykeeper.services.photo_classification_policy import (
    MemoryKeeperPhotoCategory,
    effective_photo_category,
)
from app.memorykeeper.services.place_matcher import PlaceMatchSource
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService


class MemoryKeeperPhotoCategoryService:
    SERVICE_NAME = "MemoryKeeper"
    RESOURCE_TYPE = "MemoryKeeperPhotoCategory"

    def __init__(self, db: Session) -> None:
        self.db = db
        self.history = HistoryRepository(db)
        self.changes = ChangeEventRepository(db)
        self.capture_dates = MemoryKeeperCaptureDateService(db)
        self.places = MemoryKeeperPlaceService(db)

    def update(
        self,
        payload: MemoryKeeperPhotoCategoryUpdateRequest,
    ) -> MemoryKeeperPhotoCategoryUpdateResponse:
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
            by_public_id = {
                common_file.file_id: (common_file, link)
                for common_file, link in rows
            }
            missing = [
                file_id for file_id in payload.file_ids if file_id not in by_public_id
            ]
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
                .order_by(CommonFileMetadata.file_id.asc())
                .with_for_update()
                .all()
            )
            metadata_by_file = {metadata.file_id: metadata for metadata in metadata_rows}

            for public_id in payload.file_ids:
                common_file, link = by_public_id[public_id]
                state = states_by_file.get(common_file.id)
                if state is None:
                    state = self.capture_dates.synchronize(
                        common_file=common_file,
                        service_link=link,
                        metadata=metadata_by_file.get(common_file.id),
                        state_missing_known=True,
                        initial_favorite=bool(common_file.favorite),
                    )
                    states_by_file[common_file.id] = state
                if common_file.id not in metadata_by_file:
                    metadata = CommonFileMetadata(file_id=common_file.id)
                    self.db.add(metadata)
                    self.db.flush()
                    metadata_by_file[common_file.id] = metadata

            conflicts: list[dict[str, object]] = []
            for public_id in payload.file_ids:
                common_file, _ = by_public_id[public_id]
                current = int(
                    states_by_file[common_file.id].photo_category_revision or 0
                )
                expected = payload.expected_category_revisions[public_id]
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

            updated_count = 0
            items: list[MemoryKeeperPhotoCategoryUpdateItem] = []
            for public_id in payload.file_ids:
                common_file, _ = by_public_id[public_id]
                state = states_by_file[common_file.id]
                metadata = metadata_by_file[common_file.id]
                previous_category = effective_photo_category(state)
                category_changed = self.set_category(
                    common_file=common_file,
                    state=state,
                    category=payload.photo_category,
                )

                if payload.photo_category == MemoryKeeperPhotoCategory.DAILY:
                    self.places._set_relation(
                        metadata=metadata,
                        common_file=common_file,
                        place=None,
                        source=PlaceMatchSource.USER,
                        distance_m=None,
                        touch_usage=False,
                    )
                elif (
                    previous_category == MemoryKeeperPhotoCategory.DAILY
                    and metadata.memorykeeper_place_id is None
                    and metadata.place_match_source == PlaceMatchSource.USER
                ):
                    # NORMAL is eligible for cleanup and future automatic matching.
                    self.places._set_relation(
                        metadata=metadata,
                        common_file=common_file,
                        place=None,
                        source=PlaceMatchSource.AUTO_PLACE_MATCH,
                        distance_m=None,
                        touch_usage=False,
                    )

                if category_changed:
                    updated_count += 1
                items.append(self._to_item(common_file, state, metadata))

            self.db.commit()
            for state in states_by_file.values():
                self.db.refresh(state)
            return MemoryKeeperPhotoCategoryUpdateResponse(
                items=items,
                updated_count=updated_count,
            )
        except Exception:
            self.db.rollback()
            raise

    def set_category(
        self,
        *,
        common_file: CommonFile,
        state: MemoryKeeperFileState,
        category: str,
    ) -> bool:
        """Set category without committing; callers own relation invariants."""
        current = effective_photo_category(state)
        if current == category:
            return False
        if category not in MemoryKeeperPhotoCategory.VALUES:
            raise ValueError(f"Unsupported MemoryKeeper photo category: {category}")

        state.photo_category = category
        state.photo_category_revision = int(state.photo_category_revision or 0) + 1
        state.updated_at = datetime.now(timezone.utc)
        self.history.create_histories(
            items=[
                {
                    "file_id": common_file.id,
                    "field_name": "memorykeeper_photo_category",
                    "old_value": current,
                    "new_value": category,
                    "source": "USER",
                    "priority": MetadataPriority.USER,
                    "modified_by": self.__class__.__name__,
                    "approved": True,
                }
            ],
            commit=False,
        )
        self.changes.append(
            service_name=self.SERVICE_NAME,
            resource_type=self.RESOURCE_TYPE,
            resource_id=common_file.file_id,
            operation=ChangeOperation.UPDATE,
            revision=state.photo_category_revision,
        )
        return True

    @staticmethod
    def _to_item(
        common_file: CommonFile,
        state: MemoryKeeperFileState,
        metadata: CommonFileMetadata,
    ) -> MemoryKeeperPhotoCategoryUpdateItem:
        return MemoryKeeperPhotoCategoryUpdateItem(
            file_id=common_file.file_id,
            photo_category=effective_photo_category(state),
            category_revision=int(state.photo_category_revision or 0),
            memorykeeper_place_id=metadata.memorykeeper_place_id,
            place_match_source=metadata.place_match_source,
            place_revision=int(metadata.place_match_revision or 0),
        )
