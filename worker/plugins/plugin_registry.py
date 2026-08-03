"""Plugin 자동 등록 Registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from worker.plugins.base import BasePlugin

_PLUGIN_REGISTRY: list[type[BasePlugin]] = []


def register_plugin(plugin_cls: type[BasePlugin]) -> type[BasePlugin]:
    """Plugin 클래스를 Registry에 등록한다."""
    if plugin_cls not in _PLUGIN_REGISTRY:
        _PLUGIN_REGISTRY.append(plugin_cls)
    return plugin_cls


def get_registered_plugins(
    worker_scope: str | None = None,
) -> list[type[BasePlugin]]:
    """등록된 Plugin 클래스 목록을 반환한다."""
    plugins = list(_PLUGIN_REGISTRY)
    if worker_scope is not None:
        plugins = [
            plugin_cls
            for plugin_cls in plugins
            if getattr(plugin_cls, "worker_scope", "upload") == worker_scope
        ]
    return plugins


def discover_plugins() -> None:
    """Plugin 모듈을 import하여 Registry에 자동 등록한다."""
    # Import side-effect로 BasePlugin.__init_subclass__가 등록을 수행한다.
    from worker.plugins import (  # noqa: F401
        exif_plugin,
        gps_plugin,
        hash_plugin,
        metadata_plugin,
        preview_plugin,
        storage_plugin,
        vision_plugin,
    )


def clear_registry() -> None:
    """테스트용 Registry 초기화."""
    _PLUGIN_REGISTRY.clear()
