"""최종 storage 이동과 common_files 생성을 담당한다."""

from __future__ import annotations

import mimetypes
import time

from sqlalchemy.exc import IntegrityError

from app.common.models.file import CommonFile
from app.common.models.file_metadata import CommonFileMetadata
from app.common.repositories.file_service_repository import FileServiceRepository
from app.memorykeeper.services.capture_date_service import (
    MemoryKeeperCaptureDateService,
)
from app.memorykeeper.services.place_service import MemoryKeeperPlaceService
from app.common.services.storage import StorageRuleEngine
from app.common.utils.perf import elapsed_ms, log_perf
from worker.plugins.base import BasePlugin, PluginContext


class StoragePlugin(BasePlugin):
    """최종 storage 이동과 common_files 생성을 담당한다."""

    plugin_name = "StoragePlugin"
    plugin_version = "1.0.0"
    plugin_priority = 30
    worker_scope = "upload"

    def run(self, context: PluginContext) -> None:
        if context.file_id is None:
            raise ValueError("file_id is required before storage")
        if context.job is None:
            raise ValueError("upload job is required")
        if context.incoming_path is None:
            raise ValueError("incoming_path is required")

        total_started = time.perf_counter()
        move_timings: dict[str, float] = {}

        started = time.perf_counter()
        relative_dir = StorageRuleEngine().build_path(context)
        rule_build_ms = elapsed_ms(started)

        context.original_path = context.storage_service.move_to_storage(
            incoming_path=context.job.incoming_path,
            file_id=context.file_id,
            extension=context.extension or context.incoming_path.suffix.lower(),
            relative_dir=relative_dir,
            timings=move_timings,
        )
        context.storage_path = context.original_path
        context.log("MOVE_STORAGE_COMPLETE")

        if context.restore_deleted_common_file:
            common_file = context.common_file
            if common_file is None:
                raise ValueError("deleted CommonFile is required for restore")
            common_file.original_name = context.original_name or context.incoming_path.name
            common_file.extension = context.extension
            common_file.mime_type = context.mime_type
            common_file.file_size = context.file_size
            common_file.width = context.width
            common_file.height = context.height
            common_file.original_path = context.storage_service.to_relative_path(
                context.original_path
            )
            common_file.preview_path = (
                context.storage_service.to_relative_path(context.preview_path)
                if context.preview_path is not None
                else None
            )
            common_file.thumb_path = (
                context.storage_service.to_relative_path(context.thumb_path)
                if context.thumb_path is not None
                else None
            )
            common_file.service_name = context.service_name or "MemoryKeeper"
            common_file.deleted = False
            try:
                context.db.commit()
                context.db.refresh(common_file)
            except Exception:
                context.db.rollback()
                raise
            link_created = self._ensure_service_link_and_projection(
                context,
                common_file,
                load_existing_metadata=True,
            )
            context.common_file = common_file
            context.log("COMMON_FILE_RESTORED")
            context.log("LINK_CREATED" if link_created else "LINK_EXISTS")
            log_perf(
                "storage_plugin",
                rule_build_ms=rule_build_ms,
                path_resolve_ms=move_timings.get("path_resolve_ms"),
                mkdir_ms=move_timings.get("mkdir_ms"),
                file_move_ms=move_timings.get("file_move_ms"),
                common_file_insert_ms=0,
                commit_ms=0,
                restored=True,
                elapsed_ms=elapsed_ms(total_started),
                job_id=getattr(context.job, "job_id", None),
            )
            return

        common_file = CommonFile(
            file_id=context.file_id,
            original_name=context.original_name or context.incoming_path.name,
            extension=context.extension,
            mime_type=context.mime_type,
            file_size=context.file_size,
            width=context.width,
            height=context.height,
            original_path=context.storage_service.to_relative_path(context.original_path),
            preview_path=(
                context.storage_service.to_relative_path(context.preview_path)
                if context.preview_path is not None
                else None
            ),
            thumb_path=(
                context.storage_service.to_relative_path(context.thumb_path)
                if context.thumb_path is not None
                else None
            ),
            service_name=context.service_name or "MemoryKeeper",
        )

        started = time.perf_counter()
        context.db.add(common_file)
        insert_ms = 0.0
        commit_ms = 0.0
        try:
            previous = context.db.expire_on_commit
            context.db.expire_on_commit = False
            try:
                context.db.flush()
                insert_ms = elapsed_ms(started)
                started_commit = time.perf_counter()
                context.db.commit()
                commit_ms = elapsed_ms(started_commit)
            finally:
                context.db.expire_on_commit = previous
        except IntegrityError:
            context.db.rollback()
            existing = (
                context.db.query(CommonFile)
                .filter(CommonFile.file_id == context.file_id)
                .first()
            )
            if existing is None:
                raise
            context.common_file = existing
            link_created = self._ensure_service_link_and_projection(
                context,
                existing,
                load_existing_metadata=True,
            )
            context.stop_pipeline = True
            context.log("DUPLICATE_FOUND")
            context.log("LINK_CREATED" if link_created else "LINK_EXISTS")
            if (context.service_name or "MemoryKeeper").casefold() == "memorykeeper":
                if MemoryKeeperPlaceService(context.db).auto_match_file(
                    file_id=existing.id
                ):
                    context.log("MEMORYKEEPER_PLACE_MATCHED")
            log_perf(
                "storage_plugin",
                rule_build_ms=rule_build_ms,
                path_resolve_ms=move_timings.get("path_resolve_ms"),
                mkdir_ms=move_timings.get("mkdir_ms"),
                file_move_ms=move_timings.get("file_move_ms"),
                common_file_insert_ms=0,
                commit_ms=0,
                duplicate=True,
                elapsed_ms=elapsed_ms(total_started),
                job_id=getattr(context.job, "job_id", None),
            )
            return
        except Exception:
            context.db.rollback()
            raise

        context.common_file = common_file
        context.log("COMMON_FILE_CREATED")
        link_created = self._ensure_service_link_and_projection(
            context,
            common_file,
            load_existing_metadata=False,
        )
        context.log("LINK_CREATED" if link_created else "LINK_EXISTS")
        log_perf(
            "storage_plugin",
            rule_build_ms=rule_build_ms,
            path_resolve_ms=move_timings.get("path_resolve_ms"),
            mkdir_ms=move_timings.get("mkdir_ms"),
            file_move_ms=move_timings.get("file_move_ms"),
            common_file_insert_ms=insert_ms,
            commit_ms=commit_ms,
            elapsed_ms=elapsed_ms(total_started),
            job_id=getattr(context.job, "job_id", None),
        )

    @staticmethod
    def _ensure_service_link_and_projection(
        context: PluginContext,
        common_file: CommonFile,
        *,
        load_existing_metadata: bool,
    ) -> bool:
        service_name = context.service_name or "MemoryKeeper"
        try:
            link, link_created = FileServiceRepository(context.db).ensure_link(
                file_id=common_file.id,
                service_name=service_name,
                commit=False,
            )
            state = None
            if service_name.casefold() == "memorykeeper":
                metadata = None
                if load_existing_metadata:
                    metadata = (
                        context.db.query(CommonFileMetadata)
                        .filter(CommonFileMetadata.file_id == common_file.id)
                        .first()
                    )
                state = MemoryKeeperCaptureDateService(context.db).synchronize(
                    common_file=common_file,
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
        return link_created


def guess_mime_type(extension: str | None) -> str | None:
    """확장자 기반 MIME type을 추정한다."""
    if not extension:
        return None
    mime_type, _ = mimetypes.guess_type(f"file{extension}")
    return mime_type
