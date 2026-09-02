from __future__ import annotations

from app.common.services.media_derivatives import MediaDerivativeService
from app.common.services.media_probe import MediaProbe
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

        media = context.media or MediaProbe().probe_for_service(
            context.incoming_path,
            filename=context.original_name or context.incoming_path.name,
            service_name=context.service_name,
        )
        context.media = media
        result = MediaDerivativeService(context.storage_service).generate(
            original_path=context.incoming_path,
            file_id=context.file_id,
            media=media,
        )
        context.width = result.width
        context.height = result.height
        context.preview_path = result.preview_path
        context.log("PREVIEW_CREATED" if context.preview_path else "PREVIEW_SKIPPED")
        context.thumb_path = result.thumb_path
        context.log("THUMB_CREATED" if context.thumb_path else "THUMB_SKIPPED")
        for failure in result.failures:
            context.log(f"DERIVATIVE_FAILED:{failure}")
