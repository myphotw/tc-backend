from __future__ import annotations

from app.common.repositories.metadata_repository import (
    MetadataRepository,
    MetadataSource,
)
from worker.plugins.base import BasePlugin, PluginContext


class MetadataPlugin(BasePlugin):
    """Repository 기반 현재 metadata 저장을 담당한다."""

    plugin_name = "MetadataPlugin"
    plugin_version = "1.0.0"
    plugin_priority = 40
    worker_scope = "upload"

    def run(self, context: PluginContext) -> None:
        if context.common_file is None:
            raise ValueError("common_file is required before metadata save")

        metadata = {
            "image_width": context.width,
            "image_height": context.height,
        }
        metadata.update(context.metadata)

        MetadataRepository(context.db).save_metadata(
            file_id=context.common_file.id,
            metadata=metadata,
            source=MetadataSource.SYSTEM,
        )
        context.log("METADATA_COMPLETE")
