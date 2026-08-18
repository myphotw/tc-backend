from __future__ import annotations

from app.common.models.file import CommonFile
from app.common.repositories.file_service_repository import FileServiceRepository
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
            _, link_created = FileServiceRepository(context.db).ensure_link(
                file_id=existing.id,
                service_name=context.service_name or "MemoryKeeper",
            )
            context.storage_service.delete_incoming(context.job.incoming_path)
            context.stop_pipeline = True
            context.log("DUPLICATE_FOUND")
            context.log(
                "DUPLICATE_FOUND "
                f"existing_service={existing.service_name or 'MemoryKeeper'} "
                f"requested_service={context.service_name}"
            )
            context.log("LINK_CREATED" if link_created else "LINK_EXISTS")
