from __future__ import annotations

from dataclasses import dataclass, field
import logging

from sqlalchemy.orm import Session

from app.astrojournal.models.observation_record import ObservationRecord
from app.astrojournal.models.plate_solve_job import AstroPlateSolveJob
from app.astrojournal.repositories.plate_solve_job_repository import PlateSolveJobStatus
from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_service import CommonFileService
from app.common.models.file_tag import CommonFileTag
from app.common.models.vision_job import CommonVisionJob
from app.common.repositories.vision_job_repository import VisionJobStatus
from app.common.services.storage_service import AssetDeleteStatus, StorageService

logger = logging.getLogger(__name__)


class FileCleanupStatus:
    CLEANED = "CLEANED"
    ALREADY_CLEANED = "ALREADY_CLEANED"
    PRESERVED_ACTIVE_RECORD = "PRESERVED_ACTIVE_RECORD"
    PRESERVED_OTHER_SERVICE = "PRESERVED_OTHER_SERVICE"
    PRESERVED_PROCESSING_VISION = "PRESERVED_PROCESSING_VISION"
    PRESERVED_ACTIVE_PLATE_SOLVE = "PRESERVED_ACTIVE_PLATE_SOLVE"
    ASSET_DELETE_FAILED = "ASSET_DELETE_FAILED"
    DATABASE_CLEANUP_FAILED = "DATABASE_CLEANUP_FAILED"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"


@dataclass(frozen=True)
class FileCleanupResult:
    file_id: int
    status: str
    physical_file_deleted: bool = False
    asset_results: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResetAssetCleanupResult:
    succeeded: bool
    deleted_original_count: int = 0
    deleted_preview_count: int = 0
    deleted_thumbnail_count: int = 0
    failed_file_id: int | None = None


class AstroJournalFileCleanupService:
    """Apply the shared DELETE_IF_UNREFERENCED physical file policy.

    MemoryKeeper and every non-AstroJournal service link are preservation
    references. Soft-deleted ObservationRecords and append-only history rows do
    not require physical media, but remain in the database for sync/idempotency.
    """

    SERVICE_NAME = "AstroJournal"
    SUCCESSFUL_ASSET_RESULTS = {
        AssetDeleteStatus.DELETED,
        AssetDeleteStatus.ALREADY_ABSENT,
    }

    def __init__(
        self,
        db: Session,
        *,
        storage_service: StorageService | None = None,
        service_name: str | None = None,
    ) -> None:
        self.db = db
        self.storage_service = storage_service or StorageService()
        self.service_name = service_name or self.SERVICE_NAME

    def cleanup_if_unreferenced(self, *, file_id: int) -> FileCleanupResult:
        return self.cleanup_for_reset(file_id=file_id, commit=True)

    def cleanup_for_reset(
        self,
        *,
        file_id: int,
        commit: bool = False,
    ) -> FileCleanupResult:
        """Stage Reset cleanup while reusing the established asset policy.

        With ``commit=False`` the caller owns the enclosing database
        transaction. Physical deletion still happens before DB cleanup, as in
        the single-record cleanup path, so a retry can reconcile missing files.
        """
        common_file = (
            self.db.query(CommonFile)
            .filter(CommonFile.id == file_id)
            .with_for_update()
            .first()
        )
        if common_file is None:
            return FileCleanupResult(file_id=file_id, status=FileCleanupStatus.FILE_NOT_FOUND)

        active_record_exists = False
        if self.service_name == self.SERVICE_NAME:
            active_record_exists = (
                self.db.query(ObservationRecord.id)
                .filter(ObservationRecord.file_id == file_id)
                .filter(ObservationRecord.service_name == self.SERVICE_NAME)
                .filter(ObservationRecord.deleted_at.is_(None))
                .first()
                is not None
            )
        if active_record_exists:
            return FileCleanupResult(
                file_id=file_id,
                status=FileCleanupStatus.PRESERVED_ACTIVE_RECORD,
            )

        other_service_exists = (
            self.db.query(CommonFileService.id)
            .filter(CommonFileService.file_id == file_id)
            .filter(CommonFileService.service_name != self.service_name)
            .first()
            is not None
        )
        if other_service_exists:
            try:
                self.db.query(CommonFileService).filter(
                    CommonFileService.file_id == file_id,
                    CommonFileService.service_name == self.service_name,
                ).delete(synchronize_session=False)
                if commit:
                    self.db.commit()
                else:
                    self.db.flush()
            except Exception:
                self.db.rollback()
                logger.exception(
                    "Failed to remove AstroJournal file link: common_file_id=%s",
                    file_id,
                )
                return FileCleanupResult(
                    file_id=file_id,
                    status=FileCleanupStatus.DATABASE_CLEANUP_FAILED,
                )
            return FileCleanupResult(
                file_id=file_id,
                status=FileCleanupStatus.PRESERVED_OTHER_SERVICE,
            )

        processing_vision_exists = (
            self.db.query(CommonVisionJob.id)
            .filter(CommonVisionJob.file_id == file_id)
            .filter(CommonVisionJob.deleted.is_(False))
            .filter(CommonVisionJob.status == VisionJobStatus.PROCESSING)
            .first()
            is not None
        )
        if processing_vision_exists:
            return FileCleanupResult(
                file_id=file_id,
                status=FileCleanupStatus.PRESERVED_PROCESSING_VISION,
            )

        active_plate_solve_exists = (
            self.db.query(AstroPlateSolveJob.id)
            .filter(AstroPlateSolveJob.common_file_id == file_id)
            .filter(
                AstroPlateSolveJob.status.in_(
                    [PlateSolveJobStatus.WAITING, PlateSolveJobStatus.PROCESSING]
                )
            )
            .first()
            is not None
        )
        if active_plate_solve_exists:
            return FileCleanupResult(
                file_id=file_id,
                status=FileCleanupStatus.PRESERVED_ACTIVE_PLATE_SOLVE,
            )

        if common_file.deleted and not any(
            (common_file.original_path, common_file.preview_path, common_file.thumb_path)
        ):
            return FileCleanupResult(
                file_id=file_id,
                status=FileCleanupStatus.ALREADY_CLEANED,
            )

        asset_results = self.storage_service.delete_common_file_assets(common_file)
        if any(
            result not in self.SUCCESSFUL_ASSET_RESULTS
            for result in asset_results.values()
        ):
            logger.error(
                "AstroJournal asset cleanup incomplete: common_file_id=%s results=%s",
                file_id,
                asset_results,
            )
            return FileCleanupResult(
                file_id=file_id,
                status=FileCleanupStatus.ASSET_DELETE_FAILED,
                physical_file_deleted=any(
                    result == AssetDeleteStatus.DELETED
                    for result in asset_results.values()
                ),
                asset_results=asset_results,
            )

        try:
            self.db.query(CommonFileMetadata).filter(
                CommonFileMetadata.file_id == file_id
            ).delete(synchronize_session=False)
            self.db.query(CommonFileTag).filter(
                CommonFileTag.file_id == file_id
            ).delete(synchronize_session=False)
            self.db.query(CommonVisionJob).filter(
                CommonVisionJob.file_id == file_id
            ).update({CommonVisionJob.deleted: True}, synchronize_session=False)
            self.db.query(CommonFileService).filter(
                CommonFileService.file_id == file_id,
                CommonFileService.service_name == self.service_name,
            ).delete(synchronize_session=False)

            # The row remains as a tombstone because soft-deleted ObservationRecord
            # rows retain their non-null FK and change-event history must stay valid.
            common_file.original_path = None
            common_file.preview_path = None
            common_file.thumb_path = None
            common_file.deleted = True
            if commit:
                self.db.commit()
            else:
                self.db.flush()
        except Exception:
            self.db.rollback()
            logger.exception(
                "AstroJournal CommonFile database cleanup failed: common_file_id=%s",
                file_id,
            )
            return FileCleanupResult(
                file_id=file_id,
                status=FileCleanupStatus.DATABASE_CLEANUP_FAILED,
                physical_file_deleted=any(
                    result == AssetDeleteStatus.DELETED
                    for result in asset_results.values()
                ),
                asset_results=asset_results,
            )

        return FileCleanupResult(
            file_id=file_id,
            status=FileCleanupStatus.CLEANED,
            physical_file_deleted=any(
                result == AssetDeleteStatus.DELETED
                for result in asset_results.values()
            ),
            asset_results=asset_results,
        )

    def delete_reset_assets(
        self,
        common_files: list[CommonFile],
    ) -> ResetAssetCleanupResult:
        """Delete Astro-only media before the caller's bulk DB transaction."""
        deleted = {"original": 0, "preview": 0, "thumb": 0}
        for common_file in common_files:
            results = self.storage_service.delete_common_file_assets(common_file)
            if any(
                result not in self.SUCCESSFUL_ASSET_RESULTS
                for result in results.values()
            ):
                return ResetAssetCleanupResult(
                    succeeded=False,
                    deleted_original_count=deleted["original"],
                    deleted_preview_count=deleted["preview"],
                    deleted_thumbnail_count=deleted["thumb"],
                    failed_file_id=common_file.id,
                )
            for kind in deleted:
                if results.get(kind) == AssetDeleteStatus.DELETED:
                    deleted[kind] += 1
        return ResetAssetCleanupResult(
            succeeded=True,
            deleted_original_count=deleted["original"],
            deleted_preview_count=deleted["preview"],
            deleted_thumbnail_count=deleted["thumb"],
        )

    def stage_reset_database_cleanup(
        self,
        *,
        astro_only_file_ids: list[int],
        target_file_ids: list[int],
    ) -> int:
        """Bulk-stage CommonFile cleanup without touching MemoryKeeper state."""
        if astro_only_file_ids:
            self.db.query(CommonFileMetadata).filter(
                CommonFileMetadata.file_id.in_(astro_only_file_ids)
            ).delete(synchronize_session=False)
            self.db.query(CommonFileTag).filter(
                CommonFileTag.file_id.in_(astro_only_file_ids)
            ).delete(synchronize_session=False)
            self.db.query(CommonVisionJob).filter(
                CommonVisionJob.file_id.in_(astro_only_file_ids)
            ).update(
                {CommonVisionJob.deleted: True},
                synchronize_session=False,
            )
            self.db.query(CommonFile).filter(
                CommonFile.id.in_(astro_only_file_ids)
            ).update(
                {
                    CommonFile.original_path: None,
                    CommonFile.preview_path: None,
                    CommonFile.thumb_path: None,
                    CommonFile.deleted: True,
                },
                synchronize_session=False,
            )
        removed_links = 0
        if target_file_ids:
            removed_links = self.db.query(CommonFileService).filter(
                CommonFileService.file_id.in_(target_file_ids),
                CommonFileService.service_name == self.service_name,
            ).delete(synchronize_session=False)
        self.db.flush()
        return int(removed_links or 0)
