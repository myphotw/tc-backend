from __future__ import annotations

import logging
import time

from app.common.repositories.history_repository import HistoryRepository
from app.common.repositories.api_usage_repository import ApiUsageLimitExceeded
from app.common.repositories.metadata_priority import MetadataPriority
from app.common.repositories.metadata_repository import MetadataSource
from app.common.utils.perf import elapsed_ms, log_perf
from worker.plugins.base import BasePlugin, PluginContext
from worker.plugins.plugin_registry import discover_plugins, get_registered_plugins

logger = logging.getLogger(__name__)


class PluginManager:
    """Worker plugin 실행 관리자."""

    def __init__(self, plugins: list[BasePlugin] | None = None) -> None:
        self.plugins = sorted(plugins or [], key=lambda plugin: plugin.priority())

    @classmethod
    def load_plugins(cls, worker_scope: str = "upload") -> "PluginManager":
        """
        Registry에서 Plugin을 자동 로드한다.

        Worker는 Plugin 목록을 직접 작성하지 않고 이 메서드만 호출한다.
        """
        discover_plugins()
        plugin_classes = get_registered_plugins(worker_scope=worker_scope)
        plugins: list[BasePlugin] = []
        for plugin_cls in plugin_classes:
            plugin = plugin_cls()
            if getattr(plugin, "enabled", True):
                plugins.append(plugin)
        return cls(plugins)

    def run(self, context: PluginContext) -> None:
        """등록된 plugin을 priority 순서대로 실행한다."""
        pipeline_started = time.perf_counter()
        plugin_timings: dict[str, float] = {}
        for plugin in self.plugins:
            if context.stop_pipeline:
                break

            plugin_name = plugin.plugin_name
            plugin_version = plugin.plugin_version
            if not context.is_plugin_enabled(plugin_name):
                context.log(f"PLUGIN_DISABLED {plugin_name} v{plugin_version}")
                continue

            context.log(f"PLUGIN_START {plugin_name} v{plugin_version}")
            context.notify_plugin_boundary()
            started = time.perf_counter()
            try:
                plugin.run(context)
                ms = elapsed_ms(started)
                plugin_timings[plugin_name] = ms
                log_perf(
                    "worker_plugin",
                    worker_scope=getattr(plugin, "worker_scope", "upload"),
                    plugin=plugin_name,
                    plugin_version=plugin_version,
                    elapsed_ms=ms,
                    job_id=getattr(context.job, "job_id", None),
                    vision_job_id=getattr(context.vision_job, "id", None),
                    worker_id=getattr(context, "worker_id", None),
                )
                context.log(f"PLUGIN_COMPLETE {plugin_name} v{plugin_version}")
                context.notify_plugin_boundary()
            except Exception as exc:
                ms = elapsed_ms(started)
                plugin_timings[plugin_name] = ms
                quota_deferred = isinstance(exc, ApiUsageLimitExceeded)
                if quota_deferred:
                    context.log(f"PLUGIN_DEFERRED {plugin_name} v{plugin_version}")
                else:
                    context.error_message = str(exc)
                    context.log(
                        f"PLUGIN_FAILED {plugin_name} v{plugin_version}:{exc}"
                    )
                    self._save_failure_history(
                        context,
                        plugin_name=plugin_name,
                        error=exc,
                    )
                log_perf(
                    "worker_plugin_deferred" if quota_deferred else "worker_plugin_failed",
                    plugin=plugin_name,
                    elapsed_ms=ms,
                    job_id=getattr(context.job, "job_id", None),
                )
                if quota_deferred:
                    logger.info("Worker plugin deferred: %s", plugin_name)
                else:
                    logger.exception("Worker plugin failed: %s", plugin_name)
                raise

        log_perf(
            "worker_pipeline",
            worker_scope=self.plugins[0].worker_scope if self.plugins else "unknown",
            elapsed_ms=elapsed_ms(pipeline_started),
            plugin_count=len(plugin_timings),
            **{f"{name}_ms": value for name, value in plugin_timings.items()},
            job_id=getattr(context.job, "job_id", None),
            vision_job_id=getattr(context.vision_job, "id", None),
        )

    def _save_failure_history(
        self,
        context: PluginContext,
        *,
        plugin_name: str,
        error: Exception,
    ) -> None:
        """가능한 경우 plugin 실패 이력을 history에 저장한다."""
        if context.common_file is None:
            return

        HistoryRepository(context.db).create_history(
            file_id=context.common_file.id,
            field_name="plugin_error",
            old_value=None,
            new_value=f"{plugin_name}: {error}",
            source=MetadataSource.SYSTEM,
            priority=MetadataPriority.SYSTEM,
            modified_by="worker",
            commit=True,
        )
