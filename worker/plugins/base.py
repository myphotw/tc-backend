from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.common.models.file import CommonFile
from app.common.models.upload_job import UploadJob
from app.common.models.vision_job import CommonVisionJob
from app.common.repositories.upload_job_repository import UploadJobRepository
from app.common.services.storage_service import StorageService


@dataclass
class PluginContext:
    """Worker plugin 간 공유되는 작업 컨텍스트."""

    db: Session
    storage_service: StorageService
    incoming_path: Path | None = None
    job: UploadJob | None = None
    job_repository: UploadJobRepository | None = None
    vision_job: CommonVisionJob | None = None
    file_id: str | None = None
    original_name: str | None = None
    extension: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    preview_path: Path | None = None
    thumb_path: Path | None = None
    original_path: Path | None = None
    common_file: CommonFile | None = None
    restore_deleted_common_file: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[dict[str, Any]] = field(default_factory=list)
    processing_log: list[str] = field(default_factory=list)
    storage_path: Path | None = None
    error_message: str | None = None
    stop_pipeline: bool = False
    has_gps: bool = False
    gps_lat: float | None = None
    gps_lon: float | None = None
    resolved_country: str | None = None
    resolved_province: str | None = None
    resolved_city: str | None = None
    resolved_district: str | None = None
    resolved_place: str | None = None
    plugin_enabled: dict[str, bool] = field(default_factory=dict)
    service_name: str = "MemoryKeeper"
    worker_id: str | None = None
    worker_monitor: Any | None = None

    def is_plugin_enabled(self, plugin_name: str) -> bool:
        """Plugin enable 여부를 반환한다. 기본값은 True."""
        return self.plugin_enabled.get(plugin_name, True)

    def notify_plugin_boundary(self) -> None:
        """Plugin 경계에서 throttled worker heartbeat를 호출한다."""
        monitor = self.worker_monitor
        if monitor is None:
            return
        job_id = self.job.job_id if self.job is not None else None
        on_boundary = getattr(monitor, "on_plugin_boundary", None)
        if callable(on_boundary):
            on_boundary(current_job_id=job_id)

    def log(self, message: str) -> None:
        """작업 로그를 context와 UploadJob에 기록한다."""
        self.processing_log.append(message)
        if self.job_repository is not None and self.job is not None:
            self.job_repository.append_log(self.job, message)


class BasePlugin(ABC):
    """
    Worker Plugin 기본 클래스.

    이 클래스를 상속하면 Plugin Registry에 자동 등록된다.
    """

    plugin_name: str = "BasePlugin"
    plugin_version: str = "1.0.0"
    plugin_priority: int = 100
    worker_scope: str = "upload"
    enabled: bool = True

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls is BasePlugin:
            return
        from worker.plugins.plugin_registry import register_plugin

        register_plugin(cls)

    def name(self) -> str:
        """Plugin 이름을 반환한다."""
        return self.plugin_name

    def priority(self) -> int:
        """낮을수록 먼저 실행되는 priority를 반환한다."""
        return self.plugin_priority

    @abstractmethod
    def run(self, context: PluginContext) -> None:
        """Plugin 작업을 실행한다."""


# 하위 호환을 위한 alias
IWorkerPlugin = BasePlugin
