from __future__ import annotations

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.models.file_tag import CommonFileTag
from app.common.repositories.file_service_repository import FileServiceRepository
from app.common.repositories.vision_job_repository import (
    VisionJobRepository,
    VisionJobStatus,
)
from app.memorykeeper.services.capture_date_service import (
    MemoryKeeperCaptureDateService,
)
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService
from worker.plugins.base import BasePlugin, PluginContext


class HashPlugin(BasePlugin):
    """SHA256 계산 및 중복 검사를 담당한다."""

    plugin_name = "HashPlugin"
    plugin_version = "1.0.0"
    plugin_priority = 10
    worker_scope = "upload"

    def run(self, context: PluginContext) -> None:
        if context.incoming_path is None:
            raise ValueError("incoming_path is required")
        if not context.incoming_path.exists():
            raise FileNotFoundError(f"Incoming file not found: {context.incoming_path}")
        if context.job is None:
            raise ValueError("upload job is required")

        context.file_size = context.incoming_path.stat().st_size
        context.file_id = context.storage_service.calculate_sha256(context.incoming_path)
        context.log("SHA256_COMPLETE")

        existing = (
            context.db.query(CommonFile)
            .filter(CommonFile.file_id == context.file_id)
            .first()
        )
        context.log("DUPLICATE_CHECK_COMPLETE")

        if existing is not None:
            context.common_file = existing
            if existing.deleted:
                # Physical cleanup keeps a CommonFile tombstone because deleted
                # ObservationRecords retain its FK. Re-upload restores that row
                # instead of treating the now-absent asset as a valid duplicate.
                context.restore_deleted_common_file = True
                context.log("DELETED_FILE_REUPLOAD")
                return
            service_name = context.service_name or "MemoryKeeper"
            try:
                link, link_created = FileServiceRepository(context.db).ensure_link(
                    file_id=existing.id,
                    service_name=service_name,
                    commit=False,
                )
                state = None
                if service_name.casefold() == "memorykeeper":
                    metadata = (
                        context.db.query(CommonFileMetadata)
                        .filter(CommonFileMetadata.file_id == existing.id)
                        .first()
                    )
                    state = MemoryKeeperCaptureDateService(context.db).synchronize(
                        common_file=existing,
                        service_link=link,
                        metadata=metadata,
                        state_missing_known=link_created,
                    )
                context.db.commit()
            except Exception:
                context.db.rollback()
                raise

            context.file_service_link = link
            context.memorykeeper_state = state
            context.storage_service.delete_incoming(context.job.incoming_path)
            context.stop_pipeline = True
            context.log("DUPLICATE_FOUND")
            context.log(
                "DUPLICATE_FOUND "
                f"existing_service={existing.service_name or 'MemoryKeeper'} "
                f"requested_service={context.service_name}"
            )
            context.log("LINK_CREATED" if link_created else "LINK_EXISTS")
            if service_name.casefold() == "memorykeeper":
                if link_created:
                    self._reuse_or_enqueue_vision(context, existing)
                if MemoryKeeperPlaceService(context.db).auto_match_file(
                    file_id=existing.id
                ):
                    context.log("MEMORYKEEPER_PLACE_MATCHED")

    @staticmethod
    def _reuse_or_enqueue_vision(
        context: PluginContext,
        common_file: CommonFile,
    ) -> None:
        repository = VisionJobRepository(context.db)
        blocking_status = repository.get_blocking_status(file_id=common_file.id)
        if blocking_status == VisionJobStatus.COMPLETED:
            context.log("VISION_RAW_REUSED:COMPLETED")
            return
        if blocking_status in {
            VisionJobStatus.WAITING,
            VisionJobStatus.PROCESSING,
        }:
            context.log("VISION_QUEUE_SKIPPED:ALREADY_EXISTS")
            return
        raw_exists = (
            context.db.query(CommonFileTag.id)
            .filter(CommonFileTag.file_id == common_file.id)
            .filter(CommonFileTag.source == "AI")
            .filter(CommonFileTag.deleted.is_(False))
            .first()
            is not None
        )
        if raw_exists:
            context.log("VISION_RAW_REUSED:LABELS")
            return
        repository.create(
            file_id=common_file.id,
            priority=0,
            skip_duplicate_check=True,
        )
        context.log("VISION_QUEUE_CREATED:RESET_REIMPORT")
