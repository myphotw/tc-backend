from __future__ import annotations

from pathlib import Path

from app.common.repositories.api_usage_repository import (
    ApiName,
    ApiProvider,
    ApiUsageRepository,
)
from app.common.repositories.metadata_repository import (
    MetadataRepository,
    MetadataSource,
)
from app.common.repositories.tag_repository import TagRepository
from app.common.services.api_clients.base_client import ApiClientError
from app.common.services.api_clients.google import VisionClient
from worker.plugins.base import BasePlugin, PluginContext


class VisionPlugin(BasePlugin):
    """
    Vision Queue 기반 AI 태그 생성 Plugin.

    Google Vision Label Detection 결과를 AI Tag로 자동 저장한다.
    USER Tag가 있으면 동일 의미 AI Tag는 만들지 않는다.
    """

    plugin_name = "VisionPlugin"
    plugin_version = "1.0.0"
    plugin_priority = 70
    worker_scope = "vision"

    def run(self, context: PluginContext) -> None:
        if context.common_file is None:
            raise ValueError("common_file is required before vision analysis")

        usage_repository = ApiUsageRepository(context.db)
        if not usage_repository.can_use(
            provider=ApiProvider.GOOGLE,
            api_name=ApiName.VISION,
            units=1,
        ):
            raise RuntimeError("VISION usage limit exceeded")

        image_path = self._resolve_image_path(context)
        client = VisionClient(db=context.db)

        try:
            labels = client.analyze(image_path=str(image_path))
        except ApiClientError:
            context.log("VISION_FAILED")
            raise
        except Exception:
            context.log("VISION_FAILED")
            raise

        tag_repository = TagRepository(context.db)
        saved_tags: list[dict[str, object]] = []
        for label in labels:
            if tag_repository.exists_user_tag(
                file_id=context.common_file.id,
                tag=label.name,
            ):
                continue

            tag = tag_repository.save_ai_tag(
                file_id=context.common_file.id,
                tag=label.name,
                confidence=label.confidence,
            )
            if tag is None:
                continue
            saved_tags.append(
                {
                    "tag": tag.tag,
                    "source": tag.source,
                    "confidence": tag.confidence,
                }
            )

        label_names = [label.name for label in labels]
        MetadataRepository(context.db).save_metadata(
            file_id=context.common_file.id,
            metadata={
                "reserved": f"VISION:{','.join(label_names)}",
            },
            source=MetadataSource.VISION,
            modified_by="VisionPlugin",
        )

        context.tags.extend(saved_tags)
        context.log("VISION_COMPLETE")

    def _resolve_image_path(self, context: PluginContext) -> Path:
        """Vision 분석에 사용할 이미지 경로를 결정한다."""
        if context.original_path is not None:
            return context.original_path
        if context.storage_path is not None:
            return context.storage_path
        if context.common_file is not None and context.common_file.original_path:
            return context.storage_service.resolve_storage_path(
                context.common_file.original_path
            )
        raise ValueError("original image path is required for vision analysis")
