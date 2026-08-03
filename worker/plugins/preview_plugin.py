from __future__ import annotations

from worker.plugins.base import BasePlugin, PluginContext


class PreviewPlugin(BasePlugin):
    """이미지 크기 조회와 preview/thumbnail 생성을 담당한다."""

    plugin_name = "PreviewPlugin"
    plugin_version = "1.0.0"
    plugin_priority = 20
    worker_scope = "upload"

    def run(self, context: PluginContext) -> None:
        if context.file_id is None:
            raise ValueError("file_id is required before preview generation")
        if context.incoming_path is None:
            raise ValueError("incoming_path is required")

        context.width, context.height = context.storage_service.get_image_size(
            context.incoming_path
        )
        context.preview_path = context.storage_service.save_preview(
            original_path=context.incoming_path,
            file_id=context.file_id,
            extension=context.extension or context.incoming_path.suffix.lower(),
        )
        context.log("PREVIEW_CREATED" if context.preview_path else "PREVIEW_SKIPPED")

        context.thumb_path = context.storage_service.save_thumbnail(
            original_path=context.incoming_path,
            file_id=context.file_id,
            extension=context.extension or context.incoming_path.suffix.lower(),
        )
        context.log("THUMB_CREATED" if context.thumb_path else "THUMB_SKIPPED")
