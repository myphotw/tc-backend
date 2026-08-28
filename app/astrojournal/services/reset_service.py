from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.models.plate_solve_job import AstroPlateSolveJob
from app.astrojournal.repositories.plate_solve_job_repository import PlateSolveJobStatus
from app.astrojournal.schemas.reset import (
    AstroJournalResetExecuteResponse,
    AstroJournalResetPreviewResponse,
)
from app.astrojournal.services.file_cleanup_service import (
    AstroJournalFileCleanupService,
)
from app.astrojournal.services.reset_guard import acquire_astrojournal_reset_lock
from app.common.models.file import CommonFile
from app.common.models.file_service import CommonFileService
from app.common.models.upload_job import UploadJob
from app.common.models.vision_job import CommonVisionJob
from app.common.repositories.change_event_repository import (
    ChangeEventRepository,
    ChangeOperation,
)
from app.common.repositories.upload_job_repository import UploadJobStatus
from app.common.repositories.vision_job_repository import VisionJobStatus
from app.common.services.storage_service import AssetDeleteStatus, StorageService


@dataclass(frozen=True)
class ResetScope:
    target_file_ids: list[int]
    astro_only_file_ids: list[int]
    shared_file_ids: list[int]


class AstroJournalResetService:
    SERVICE_NAME = "AstroJournal"
    RESOURCE_TYPE = "AstroJournalReset"
    RESOURCE_ID = "AstroJournal"

    def __init__(
        self,
        db: Session,
        *,
        cleanup_service: AstroJournalFileCleanupService | None = None,
        storage_service: StorageService | None = None,
    ) -> None:
        self.db = db
        self.cleanup_service = cleanup_service or AstroJournalFileCleanupService(db)
        self.storage_service = storage_service or self.cleanup_service.storage_service

    def preview(self) -> AstroJournalResetPreviewResponse:
        scope = self._scope()
        processing_upload_count = self._processing_upload_jobs().count()
        processing_vision_count = self._processing_vision_jobs(
            scope.astro_only_file_ids
        ).count()
        processing_plate_solve_count = self._processing_plate_solve_jobs().count()
        processing_job_count = (
            processing_upload_count
            + processing_vision_count
            + processing_plate_solve_count
        )
        astro_only_files = self._common_files(scope.astro_only_file_ids)
        return AstroJournalResetPreviewResponse(
            observation_record_count=(
                self.db.query(ObservationRecord.id)
                .filter(ObservationRecord.service_name == self.SERVICE_NAME)
                .count()
            ),
            astro_file_count=len(scope.target_file_ids),
            astro_only_file_count=len(scope.astro_only_file_ids),
            shared_file_count=len(scope.shared_file_ids),
            # Persistent Plate Solve retention is not part of Reset yet.
            # PhotoObject has no Backend persistence model in the current schema.
            plate_solve_result_count=0,
            photo_object_count=0,
            upload_job_count=self._upload_jobs().count(),
            pending_upload_count=self._pending_upload_jobs().count(),
            processing_upload_count=processing_upload_count,
            processing_vision_job_count=processing_vision_count,
            processing_job_count=processing_job_count,
            physical_original_delete_count=sum(
                bool(item.original_path) for item in astro_only_files
            ),
            physical_preview_delete_count=sum(
                bool(item.preview_path) for item in astro_only_files
            ),
            physical_thumbnail_delete_count=sum(
                bool(item.thumb_path) for item in astro_only_files
            ),
            preserved_shared_file_count=len(scope.shared_file_ids),
            reset_blocked=processing_job_count > 0,
            blocked_reason=(
                "PROCESSING_JOBS" if processing_job_count > 0 else None
            ),
        )

    def execute(self) -> AstroJournalResetExecuteResponse:
        try:
            acquire_astrojournal_reset_lock(self.db, exclusive=True)
            self._lock_scope()
            snapshot = self.preview()
            if snapshot.reset_blocked:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "ASTROJOURNAL_RESET_BLOCKED",
                        "processing_upload_count": snapshot.processing_upload_count,
                        "processing_vision_job_count": (
                            snapshot.processing_vision_job_count
                        ),
                    },
                )

            scope = self._scope()
            astro_only_files = self._common_files(scope.astro_only_file_ids)

            self._plate_solve_jobs().filter(
                AstroPlateSolveJob.status == PlateSolveJobStatus.WAITING
            ).update(
                {
                    AstroPlateSolveJob.status: PlateSolveJobStatus.FAILED,
                    AstroPlateSolveJob.completed_at: datetime.now(timezone.utc),
                    AstroPlateSolveJob.last_error: (
                        "AstroJournal Reset occurred before Plate Solve started"
                    ),
                },
                synchronize_session=False,
            )

            self._delete_upload_incoming_assets()
            asset_cleanup = self.cleanup_service.delete_reset_assets(
                astro_only_files
            )
            if not asset_cleanup.succeeded:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={"code": "ASTROJOURNAL_RESET_CLEANUP_FAILED"},
                )

            deleted_observation_count = (
                self.db.query(ObservationRecord)
                .filter(ObservationRecord.service_name == self.SERVICE_NAME)
                .delete(synchronize_session=False)
            )
            removed_links = self.cleanup_service.stage_reset_database_cleanup(
                astro_only_file_ids=scope.astro_only_file_ids,
                target_file_ids=scope.target_file_ids,
            )
            deleted_upload_count = self._upload_jobs().delete(
                synchronize_session=False
            )

            # Preserve the append-only cursor log and emit one high-level
            # invalidation instead of thousands of per-record tombstones.
            event = ChangeEventRepository(self.db).append(
                service_name=self.SERVICE_NAME,
                resource_type=self.RESOURCE_TYPE,
                resource_id=self.RESOURCE_ID,
                operation=ChangeOperation.UPDATE,
                revision=None,
            )
            self.db.commit()
            self.db.expire_all()
            return AstroJournalResetExecuteResponse(
                reset_completed=True,
                deleted_observation_record_count=int(
                    deleted_observation_count or 0
                ),
                removed_astro_file_link_count=removed_links,
                tombstoned_common_file_count=len(scope.astro_only_file_ids),
                preserved_shared_file_count=len(scope.shared_file_ids),
                deleted_upload_job_count=int(deleted_upload_count or 0),
                deleted_original_count=asset_cleanup.deleted_original_count,
                deleted_preview_count=asset_cleanup.deleted_preview_count,
                deleted_thumbnail_count=asset_cleanup.deleted_thumbnail_count,
                deleted_plate_solve_result_count=0,
                deleted_photo_object_count=0,
                reset_event_cursor=int(event.id),
            )
        except HTTPException:
            self.db.rollback()
            raise
        except Exception:
            self.db.rollback()
            raise

    def _scope(self) -> ResetScope:
        linked_ids = {
            int(file_id)
            for (file_id,) in (
                self.db.query(CommonFileService.file_id)
                .filter(CommonFileService.service_name == self.SERVICE_NAME)
                .all()
            )
        }
        record_ids = {
            int(file_id)
            for (file_id,) in (
                self.db.query(ObservationRecord.file_id)
                .filter(ObservationRecord.service_name == self.SERVICE_NAME)
                .distinct()
                .all()
            )
        }
        target_ids = linked_ids | record_ids
        if not target_ids:
            return ResetScope([], [], [])
        existing_ids = {
            int(file_id)
            for (file_id,) in (
                self.db.query(CommonFile.id)
                .filter(CommonFile.id.in_(target_ids))
                .all()
            )
        }
        shared_ids = {
            int(file_id)
            for (file_id,) in (
                self.db.query(CommonFileService.file_id)
                .filter(CommonFileService.file_id.in_(existing_ids))
                .filter(CommonFileService.service_name != self.SERVICE_NAME)
                .distinct()
                .all()
            )
        }
        return ResetScope(
            target_file_ids=sorted(existing_ids),
            astro_only_file_ids=sorted(existing_ids - shared_ids),
            shared_file_ids=sorted(shared_ids),
        )

    def _lock_scope(self) -> None:
        self._upload_jobs().with_for_update().all()
        self._plate_solve_jobs().with_for_update().all()
        (
            self.db.query(ObservationRecord)
            .filter(ObservationRecord.service_name == self.SERVICE_NAME)
            .with_for_update()
            .all()
        )
        (
            self.db.query(CommonFileService)
            .filter(CommonFileService.service_name == self.SERVICE_NAME)
            .with_for_update()
            .all()
        )
        scope = self._scope()
        if scope.target_file_ids:
            (
                self.db.query(CommonFile)
                .filter(CommonFile.id.in_(scope.target_file_ids))
                .with_for_update()
                .all()
            )
        if scope.astro_only_file_ids:
            (
                self.db.query(CommonVisionJob)
                .filter(CommonVisionJob.file_id.in_(scope.astro_only_file_ids))
                .filter(CommonVisionJob.deleted.is_(False))
                .with_for_update()
                .all()
            )

    def _common_files(self, file_ids: list[int]) -> list[CommonFile]:
        if not file_ids:
            return []
        return (
            self.db.query(CommonFile)
            .filter(CommonFile.id.in_(file_ids))
            .order_by(CommonFile.id.asc())
            .all()
        )

    def _upload_jobs(self):
        return self.db.query(UploadJob).filter(
            UploadJob.service_name == self.SERVICE_NAME
        )

    def _pending_upload_jobs(self):
        return self._upload_jobs().filter(
            UploadJob.status == UploadJobStatus.WAITING
        )

    def _processing_upload_jobs(self):
        return self._upload_jobs().filter(
            UploadJob.status == UploadJobStatus.PROCESSING
        )

    def _processing_vision_jobs(self, astro_only_file_ids: list[int]):
        query = self.db.query(CommonVisionJob).filter(False)
        if astro_only_file_ids:
            query = (
                self.db.query(CommonVisionJob)
                .filter(CommonVisionJob.file_id.in_(astro_only_file_ids))
                .filter(CommonVisionJob.deleted.is_(False))
                .filter(CommonVisionJob.status == VisionJobStatus.PROCESSING)
            )
        return query

    def _plate_solve_jobs(self):
        return self.db.query(AstroPlateSolveJob)

    def _processing_plate_solve_jobs(self):
        return self._plate_solve_jobs().filter(
            AstroPlateSolveJob.status == PlateSolveJobStatus.PROCESSING
        )

    def _delete_upload_incoming_assets(self) -> None:
        for job in self._upload_jobs().all():
            result = self.storage_service.delete_incoming_asset(job.incoming_path)
            if result not in {
                AssetDeleteStatus.DELETED,
                AssetDeleteStatus.ALREADY_ABSENT,
            }:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={"code": "ASTROJOURNAL_RESET_CLEANUP_FAILED"},
                )
