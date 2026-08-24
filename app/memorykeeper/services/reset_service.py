from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Session, aliased

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.models.metadata_history import CommonMetadataHistory
from app.common.models.upload_job import UploadJob
from app.common.models.vision_job import CommonVisionJob
from app.common.repositories.change_event_repository import (
    ChangeEventRepository,
    ChangeOperation,
)
from app.common.repositories.upload_job_repository import UploadJobStatus
from app.common.repositories.vision_job_repository import VisionJobStatus
from app.memorykeeper.models.file_state import MemoryKeeperFileState
from app.memorykeeper.models.file_tag_suppression import (
    MemoryKeeperFileTagSuppression,
)
from app.memorykeeper.models.photo_tag import PhotoTag
from app.memorykeeper.models.place import MemoryKeeperPlace
from app.memorykeeper.models.tag import Tag
from app.memorykeeper.models.tag_canonical_override import (
    MemoryKeeperTagCanonicalOverride,
)
from app.memorykeeper.schemas.reset import (
    MemoryKeeperResetExecuteResponse,
    MemoryKeeperResetPreviewResponse,
)
from app.memorykeeper.services.reset_guard import (
    acquire_memorykeeper_reset_lock,
)


class MemoryKeeperResetService:
    SERVICE_NAME = "MemoryKeeper"
    RESOURCE_TYPE = "MemoryKeeperReset"
    RESOURCE_ID = "MemoryKeeper"
    SEMANTIC_HISTORY_FIELDS = (
        "memorykeeper_favorite",
        "memorykeeper_memo",
        "memorykeeper_place_id",
        "place_match_source",
        "place_match_distance_m",
        "place_match_revision",
    )

    def __init__(self, db: Session) -> None:
        self.db = db

    def preview(self) -> MemoryKeeperResetPreviewResponse:
        memorykeeper_ids = self._memorykeeper_file_ids()
        memorykeeper_id_subquery = memorykeeper_ids.statement
        memorykeeper_file_count = memorykeeper_ids.count()
        active_upload_job_count = self._active_upload_jobs().count()
        processing_vision_job_count = self._processing_memorykeeper_only_vision().count()
        return MemoryKeeperResetPreviewResponse(
            memorykeeper_file_count=memorykeeper_file_count,
            place_count=self.db.query(MemoryKeeperPlace.id).count(),
            user_tag_count=self.db.query(Tag.id).count(),
            favorite_count=(
                self.db.query(MemoryKeeperFileState.file_id)
                .filter(MemoryKeeperFileState.favorite.is_(True))
                .count()
            ),
            memo_count=(
                self.db.query(MemoryKeeperFileState.file_id)
                .filter(MemoryKeeperFileState.memo.is_not(None))
                .filter(func.length(func.trim(MemoryKeeperFileState.memo)) > 0)
                .count()
            ),
            file_tag_relation_count=(
                self.db.query(CommonFileTag.id)
                .filter(
                    or_(
                        CommonFileTag.memorykeeper_tag_id.is_not(None),
                        (
                            (CommonFileTag.source == "USER")
                            & CommonFileTag.file_id.in_(memorykeeper_id_subquery)
                        ),
                    )
                )
                .count()
            ),
            file_tag_suppression_count=(
                self.db.query(MemoryKeeperFileTagSuppression.id).count()
            ),
            pending_count=self._pending_count(memorykeeper_id_subquery),
            preserved_common_file_count=memorykeeper_file_count,
            preserved_raw_vision_count=self._raw_vision_count(
                memorykeeper_id_subquery
            ),
            shared_with_other_service_count=self._shared_file_count(
                memorykeeper_id_subquery
            ),
            upload_job_count=(
                self.db.query(UploadJob.id)
                .filter(UploadJob.service_name == self.SERVICE_NAME)
                .count()
            ),
            active_upload_job_count=active_upload_job_count,
            processing_vision_job_count=processing_vision_job_count,
            reset_blocked=bool(
                active_upload_job_count or processing_vision_job_count
            ),
        )

    def execute(self) -> MemoryKeeperResetExecuteResponse:
        try:
            acquire_memorykeeper_reset_lock(self.db, exclusive=True)
            self._lock_reset_scope()
            self._raise_if_processing()

            snapshot = self.preview()
            memorykeeper_id_subquery = self._memorykeeper_file_ids().statement
            memorykeeper_only_id_subquery = (
                self._memorykeeper_only_file_ids().statement
            )

            # Jobs without another service consumer must not spend quota after
            # their MemoryKeeper link is removed. COMPLETED results stay active.
            (
                self.db.query(CommonVisionJob)
                .filter(CommonVisionJob.file_id.in_(memorykeeper_only_id_subquery))
                .filter(CommonVisionJob.deleted.is_(False))
                .filter(
                    CommonVisionJob.status.in_(
                        [
                            VisionJobStatus.WAITING,
                            VisionJobStatus.FAILED,
                            VisionJobStatus.SKIPPED,
                        ]
                    )
                )
                .update(
                    {CommonVisionJob.deleted: True},
                    synchronize_session=False,
                )
            )

            # Only MemoryKeeper semantic projection fields are cleared. Raw
            # EXIF/GPS/geocoding and Astro columns remain untouched.
            (
                self.db.query(CommonFileMetadata)
                .filter(
                    or_(
                        CommonFileMetadata.memorykeeper_place_id.is_not(None),
                        CommonFileMetadata.place_match_source.is_not(None),
                        CommonFileMetadata.place_match_distance_m.is_not(None),
                        CommonFileMetadata.place_match_revision != 0,
                    )
                )
                .update(
                    {
                        CommonFileMetadata.memorykeeper_place_id: None,
                        CommonFileMetadata.place_match_source: None,
                        CommonFileMetadata.place_match_distance_m: None,
                        CommonFileMetadata.place_match_revision: 0,
                    },
                    synchronize_session=False,
                )
            )
            (
                self.db.query(CommonMetadataHistory)
                .filter(
                    CommonMetadataHistory.field_name.in_(
                        self.SEMANTIC_HISTORY_FIELDS
                    )
                )
                .delete(synchronize_session=False)
            )

            self.db.query(MemoryKeeperFileTagSuppression).delete(
                synchronize_session=False
            )
            self.db.query(MemoryKeeperTagCanonicalOverride).delete(
                synchronize_session=False
            )
            (
                self.db.query(CommonFileTag)
                .filter(
                    or_(
                        CommonFileTag.memorykeeper_tag_id.is_not(None),
                        (
                            (CommonFileTag.source == "USER")
                            & CommonFileTag.file_id.in_(
                                memorykeeper_id_subquery
                            )
                        ),
                    )
                )
                .delete(synchronize_session=False)
            )
            # Legacy relation rows still reference the same USER tag master.
            self.db.query(PhotoTag).delete(synchronize_session=False)
            self.db.query(Tag).delete(synchronize_session=False)
            cleared_state_count = self.db.query(MemoryKeeperFileState).delete(
                synchronize_session=False
            )
            removed_place_count = self.db.query(MemoryKeeperPlace).delete(
                synchronize_session=False
            )

            # Old MemoryKeeper idempotency jobs would otherwise intercept a
            # deliberate re-import and prevent recreation of the service link.
            (
                self.db.query(UploadJob)
                .filter(UploadJob.service_name == self.SERVICE_NAME)
                .delete(synchronize_session=False)
            )
            (
                self.db.query(CommonFileService)
                .filter(CommonFileService.service_name == self.SERVICE_NAME)
                .delete(synchronize_session=False)
            )

            event = ChangeEventRepository(self.db).append(
                service_name=self.SERVICE_NAME,
                resource_type=self.RESOURCE_TYPE,
                resource_id=self.RESOURCE_ID,
                operation=ChangeOperation.UPDATE,
                revision=None,
            )
            self.db.commit()
            return MemoryKeeperResetExecuteResponse(
                reset_completed=True,
                affected_file_count=snapshot.memorykeeper_file_count,
                removed_place_count=removed_place_count,
                removed_user_tag_count=snapshot.user_tag_count,
                cleared_state_count=cleared_state_count,
                preserved_common_file_count=snapshot.preserved_common_file_count,
                preserved_raw_vision_count=snapshot.preserved_raw_vision_count,
                reset_event_cursor=int(event.id),
            )
        except HTTPException:
            self.db.rollback()
            raise
        except Exception:
            self.db.rollback()
            raise

    def _lock_reset_scope(self) -> None:
        # Existing rows are locked so a worker cannot transition them between
        # the processing check and semantic deletion.
        self._active_upload_jobs().with_for_update().all()
        (
            self.db.query(CommonFileService)
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .with_for_update()
            .all()
        )
        (
            self.db.query(CommonVisionJob)
            .filter(
                CommonVisionJob.file_id.in_(
                    self._memorykeeper_only_file_ids().statement
                )
            )
            .filter(CommonVisionJob.deleted.is_(False))
            .with_for_update()
            .all()
        )

    def _raise_if_processing(self) -> None:
        upload_count = self._active_upload_jobs().count()
        vision_count = self._processing_memorykeeper_only_vision().count()
        if not upload_count and not vision_count:
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "MEMORYKEEPER_RESET_BLOCKED",
                "active_upload_job_count": upload_count,
                "processing_vision_job_count": vision_count,
            },
        )

    def _memorykeeper_file_ids(self):
        return (
            self.db.query(CommonFileService.file_id)
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
        )

    def _memorykeeper_only_file_ids(self):
        other_link = aliased(CommonFileService)
        return self._memorykeeper_file_ids().filter(
            ~exists().where(
                (other_link.file_id == CommonFileService.file_id)
                & (other_link.service_name != self.SERVICE_NAME)
            )
        )

    def _active_upload_jobs(self):
        return (
            self.db.query(UploadJob)
            .filter(UploadJob.service_name == self.SERVICE_NAME)
            .filter(
                UploadJob.status.in_(
                    [UploadJobStatus.WAITING, UploadJobStatus.PROCESSING]
                )
            )
        )

    def _processing_memorykeeper_only_vision(self):
        return (
            self.db.query(CommonVisionJob)
            .filter(
                CommonVisionJob.file_id.in_(
                    self._memorykeeper_only_file_ids().statement
                )
            )
            .filter(CommonVisionJob.deleted.is_(False))
            .filter(CommonVisionJob.status == VisionJobStatus.PROCESSING)
        )

    def _pending_count(self, memorykeeper_ids) -> int:
        return (
            self.db.query(CommonFile.id)
            .outerjoin(
                CommonFileMetadata,
                CommonFileMetadata.file_id == CommonFile.id,
            )
            .filter(CommonFile.id.in_(memorykeeper_ids))
            .filter(CommonFile.deleted.is_(False))
            .filter(CommonFileMetadata.memorykeeper_place_id.is_(None))
            .count()
        )

    def _raw_vision_count(self, memorykeeper_ids) -> int:
        raw_label_files = (
            self.db.query(CommonFileTag.file_id.label("file_id"))
            .filter(CommonFileTag.file_id.in_(memorykeeper_ids))
            .filter(CommonFileTag.source == "AI")
            .filter(CommonFileTag.deleted.is_(False))
        )
        completed_files = (
            self.db.query(CommonVisionJob.file_id.label("file_id"))
            .filter(CommonVisionJob.file_id.in_(memorykeeper_ids))
            .filter(CommonVisionJob.status == VisionJobStatus.COMPLETED)
            .filter(CommonVisionJob.deleted.is_(False))
        )
        # UNION makes this a reusable-result file count, not a raw-label row
        # count. A valid completed zero-label analysis is therefore included.
        return raw_label_files.union(completed_files).count()

    def _shared_file_count(self, memorykeeper_ids) -> int:
        return (
            self.db.query(func.count(func.distinct(CommonFileService.file_id)))
            .filter(CommonFileService.file_id.in_(memorykeeper_ids))
            .filter(CommonFileService.service_name != self.SERVICE_NAME)
            .scalar()
            or 0
        )
