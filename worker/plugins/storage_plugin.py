from __future__ import annotations

import mimetypes

from sqlalchemy.exc import IntegrityError

from app.common.models.file import CommonFile
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

        context.original_path = context.storage_service.move_to_storage(
            incoming_path=context.job.incoming_path,
            file_id=context.file_id,
            extension=context.extension or context.incoming_path.suffix.lower(),
        )
        context.storage_path = context.original_path
        context.log("MOVE_STORAGE_COMPLETE")

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
        )

        context.db.add(common_file)
        try:
            context.db.commit()
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
            context.stop_pipeline = True
            context.log("DUPLICATE_FOUND")
            return

        context.db.refresh(common_file)
        context.common_file = common_file
        context.log("COMMON_FILE_CREATED")


def guess_mime_type(extension: str | None) -> str | None:
    """확장자 기반 MIME type을 추정한다."""
    if not extension:
        return None
    mime_type, _ = mimetypes.guess_type(f"file{extension}")
    return mime_type
