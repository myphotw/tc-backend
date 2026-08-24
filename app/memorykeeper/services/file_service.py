from __future__ import annotations

from datetime import datetime, timezone
import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.astrojournal.services.file_cleanup_service import (
    AstroJournalFileCleanupService,
    FileCleanupResult,
    FileCleanupStatus,
)
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.repositories.change_event_repository import ChangeEventRepository, ChangeOperation
from app.common.repositories.history_repository import HistoryRepository
from app.common.repositories.metadata_priority import MetadataPriority
from app.common.repositories.tag_repository import TagSource
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.file_tag_suppression import (
    MemoryKeeperFileTagSuppression,
)
from app.memorykeeper.schemas.file import (
    MemoryKeeperFileDeleteResponse,
    MemoryKeeperFileMetadataResponse,
    MemoryKeeperFileMetadataUpdate,
)
from app.memorykeeper.services.place_matcher import PlaceMatchSource
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService

logger = logging.getLogger(__name__)


class MemoryKeeperFileService:
    SERVICE_NAME = "MemoryKeeper"
    RESOURCE_TYPE = "MemoryKeeperFile"
    METADATA_RESOURCE_TYPE = "MemoryKeeperFileMetadata"
    SUCCESSFUL_CLEANUP = {
        FileCleanupStatus.CLEANED,
        FileCleanupStatus.ALREADY_CLEANED,
        FileCleanupStatus.PRESERVED_OTHER_SERVICE,
    }

    def __init__(
        self,
        db: Session,
        *,
        cleanup_service: AstroJournalFileCleanupService | None = None,
    ) -> None:
        self.db = db
        self.history = HistoryRepository(db)
        self.changes = ChangeEventRepository(db)
        self.cleanup_service = cleanup_service or AstroJournalFileCleanupService(
            db,
            service_name=self.SERVICE_NAME,
        )
        self.last_cleanup_result: FileCleanupResult | None = None

    def patch_metadata(
        self,
        public_file_id: str,
        payload: MemoryKeeperFileMetadataUpdate,
    ) -> MemoryKeeperFileMetadataResponse:
        common_file = self.require_file(public_file_id, lock=True)
        state = self.get_state(common_file, create=True)
        if int(state.revision or 0) != payload.expected_revision:
            self._revision_conflict(common_file, payload.expected_revision, int(state.revision or 0))

        metadata = self.get_metadata(common_file, create=True)
        values = payload.model_dump(exclude={"expected_revision"}, exclude_unset=True)
        history_items: list[dict[str, object]] = []

        if "favorite" in values:
            self._history_item(
                history_items,
                file_id=common_file.id,
                field_name="memorykeeper_favorite",
                old_value=bool(state.favorite),
                new_value=bool(values["favorite"]),
            )
            state.favorite = bool(values.pop("favorite"))
        if "memo" in values:
            self._history_item(
                history_items,
                file_id=common_file.id,
                field_name="memorykeeper_memo",
                old_value=state.memo,
                new_value=values["memo"],
            )
            state.memo = values.pop("memo")

        gps_changed = "gps_lat" in values or "gps_lon" in values
        for field_name, new_value in values.items():
            old_value = getattr(metadata, field_name)
            self._history_item(
                history_items,
                file_id=common_file.id,
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
            )
            setattr(metadata, field_name, new_value)
        if values:
            metadata.locked = True

        if history_items:
            self.history.create_histories(items=history_items, commit=False)

        if gps_changed:
            self._reconcile_place_after_gps_patch(common_file, metadata)

        state.revision = int(state.revision or 0) + 1
        state.updated_at = datetime.now(timezone.utc)
        self.changes.append(
            service_name=self.SERVICE_NAME,
            resource_type=self.METADATA_RESOURCE_TYPE,
            resource_id=common_file.file_id,
            operation=ChangeOperation.UPDATE,
            revision=state.revision,
        )
        self.db.commit()
        self.db.refresh(state)
        self.db.refresh(metadata)
        return self.to_metadata_response(common_file, metadata, state)

    def delete(self, public_file_id: str) -> MemoryKeeperFileDeleteResponse:
        common_file = self.require_file(public_file_id, lock=True)
        state = self.get_state(common_file, create=False)
        revision = int(state.revision or 0) + 1 if state is not None else 1
        metadata = self.get_metadata(common_file, create=False)

        if metadata is not None and metadata.memorykeeper_place_id is not None:
            place_service = MemoryKeeperPlaceService(self.db)
            place_service._set_relation(
                metadata=metadata,
                common_file=common_file,
                place=None,
                source=PlaceMatchSource.USER,
                distance_m=None,
                touch_usage=False,
            )

        user_relations = (
            self.db.query(CommonFileTag)
            .filter(CommonFileTag.file_id == common_file.id)
            .filter(CommonFileTag.source == TagSource.USER)
            .filter(CommonFileTag.deleted.is_(False))
            .all()
        )
        for relation in user_relations:
            relation.deleted = True
            self.changes.append(
                service_name=self.SERVICE_NAME,
                resource_type="MemoryKeeperFileTag",
                resource_id=f"{common_file.file_id}:{relation.memorykeeper_tag_id or relation.id}",
                operation=ChangeOperation.DELETE,
                revision=revision,
                tombstone=True,
            )
        file_suppressions = (
            self.db.query(MemoryKeeperFileTagSuppression)
            .filter(MemoryKeeperFileTagSuppression.file_id == common_file.id)
            .filter(MemoryKeeperFileTagSuppression.deleted.is_(False))
            .all()
        )
        for suppression in file_suppressions:
            suppression.deleted = True
            suppression.revision = int(suppression.revision or 0) + 1
            suppression.updated_at = datetime.now(timezone.utc)
        if state is not None:
            self.db.delete(state)
        self.changes.append(
            service_name=self.SERVICE_NAME,
            resource_type=self.RESOURCE_TYPE,
            resource_id=common_file.file_id,
            operation=ChangeOperation.DELETE,
            revision=revision,
            tombstone=True,
        )

        try:
            result = self.cleanup_service.cleanup_if_unreferenced(file_id=common_file.id)
        except Exception as exc:
            self.db.rollback()
            logger.exception("MemoryKeeper file cleanup failed: file_id=%s", common_file.file_id)
            raise HTTPException(status_code=503, detail="File cleanup failed") from exc
        self.last_cleanup_result = result
        if result.status not in self.SUCCESSFUL_CLEANUP:
            self.db.rollback()
            code = 409 if result.status in {
                FileCleanupStatus.PRESERVED_ACTIVE_RECORD,
                FileCleanupStatus.PRESERVED_PROCESSING_VISION,
            } else 503
            raise HTTPException(
                status_code=code,
                detail={"code": "FILE_CLEANUP_INCOMPLETE", "cleanup_status": result.status},
            )
        return MemoryKeeperFileDeleteResponse(
            file_id=common_file.file_id,
            cleanup_status=result.status,
            physical_file_deleted=result.physical_file_deleted,
        )

    def require_file(self, public_file_id: str, *, lock: bool = False) -> CommonFile:
        query = (
            self.db.query(CommonFile)
            .join(CommonFileService, CommonFileService.file_id == CommonFile.id)
            .filter(CommonFile.file_id == public_file_id)
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
        )
        if lock:
            query = query.with_for_update()
        common_file = query.first()
        if common_file is None:
            raise HTTPException(status_code=404, detail="MemoryKeeper file not found")
        return common_file

    def get_state(
        self,
        common_file: CommonFile,
        *,
        create: bool,
    ) -> MemoryKeeperFileState | None:
        state = self.db.get(MemoryKeeperFileState, common_file.id)
        if state is None and create:
            state = MemoryKeeperFileState(
                file_id=common_file.id,
                favorite=bool(common_file.favorite),
                memo=None,
                revision=0,
            )
            self.db.add(state)
            self.db.flush()
        return state

    def get_metadata(
        self,
        common_file: CommonFile,
        *,
        create: bool,
    ) -> CommonFileMetadata | None:
        metadata = (
            self.db.query(CommonFileMetadata)
            .filter(CommonFileMetadata.file_id == common_file.id)
            .first()
        )
        if metadata is None and create:
            metadata = CommonFileMetadata(file_id=common_file.id)
            self.db.add(metadata)
            self.db.flush()
        return metadata

    @staticmethod
    def to_metadata_response(
        common_file: CommonFile,
        metadata: CommonFileMetadata,
        state: MemoryKeeperFileState,
    ) -> MemoryKeeperFileMetadataResponse:
        return MemoryKeeperFileMetadataResponse(
            file_id=common_file.file_id,
            favorite=bool(state.favorite),
            memo=state.memo,
            revision=int(state.revision or 0),
            gps_lat=metadata.gps_lat,
            gps_lon=metadata.gps_lon,
            country=metadata.country,
            province=metadata.province,
            city=metadata.city,
            district=metadata.district,
            place_name=metadata.place_name,
            memorykeeper_place_id=metadata.memorykeeper_place_id,
            place_match_source=metadata.place_match_source,
            place_match_distance_m=metadata.place_match_distance_m,
            place_revision=int(metadata.place_match_revision or 0),
            updated_at=state.updated_at,
        )

    def _reconcile_place_after_gps_patch(
        self,
        common_file: CommonFile,
        metadata: CommonFileMetadata,
    ) -> None:
        place_service = MemoryKeeperPlaceService(self.db)
        if metadata.gps_lat is None or metadata.gps_lon is None:
            if metadata.place_match_source == PlaceMatchSource.USER:
                current = (
                    place_service.repository.get(metadata.memorykeeper_place_id)
                    if metadata.memorykeeper_place_id
                    else None
                )
                if current is not None:
                    place_service._set_relation(
                        metadata=metadata,
                        common_file=common_file,
                        place=current,
                        source=PlaceMatchSource.USER,
                        distance_m=None,
                        touch_usage=False,
                    )
            else:
                place_service._set_relation(
                    metadata=metadata,
                    common_file=common_file,
                    place=None,
                    source=PlaceMatchSource.AUTO_PLACE_MATCH,
                    distance_m=None,
                    touch_usage=False,
                )
            return

        current = (
            place_service.repository.get(metadata.memorykeeper_place_id)
            if metadata.memorykeeper_place_id
            else None
        )
        if metadata.place_match_source == PlaceMatchSource.USER:
            if current is not None:
                place_service._set_relation(
                    metadata=metadata,
                    common_file=common_file,
                    place=current,
                    source=PlaceMatchSource.USER,
                    distance_m=place_service._distance(metadata, current),
                    touch_usage=False,
                )
            return

        match = place_service.matcher.match(
            gps_lat=float(metadata.gps_lat),
            gps_lon=float(metadata.gps_lon),
            canonical_name=metadata.place_name,
        )
        place_service._set_relation(
            metadata=metadata,
            common_file=common_file,
            place=match.place,
            source=(match.source if match.place is not None else PlaceMatchSource.AUTO_PLACE_MATCH),
            distance_m=match.distance_m,
            touch_usage=match.place is not None,
        )

    @staticmethod
    def _history_item(
        items: list[dict[str, object]],
        *,
        file_id: int,
        field_name: str,
        old_value: object,
        new_value: object,
    ) -> None:
        if old_value == new_value:
            return
        items.append(
            {
                "file_id": file_id,
                "field_name": field_name,
                "old_value": old_value,
                "new_value": new_value,
                "source": "USER",
                "priority": MetadataPriority.USER,
                "modified_by": "MemoryKeeperFileService",
                "approved": True,
            }
        )

    @staticmethod
    def _revision_conflict(
        common_file: CommonFile,
        expected: int,
        current: int,
    ) -> None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "REVISION_CONFLICT",
                "file_id": common_file.file_id,
                "expected_revision": expected,
                "current_revision": current,
            },
        )
