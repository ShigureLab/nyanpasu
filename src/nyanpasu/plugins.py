from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from importlib import metadata
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel

from nyanpasu.models import AgentTask, TaskRunResult

if TYPE_CHECKING:
    from fastapi import APIRouter

    from nyanpasu.config import NyanpasuConfig

PostProcessHook = Callable[[AgentTask, TaskRunResult], Awaitable[None]]


@runtime_checkable
class NyanpasuPlugin(Protocol):
    id: str
    config_model: type[BaseModel] | None

    async def setup(self, runtime: PluginRuntime, config: BaseModel | dict[str, Any]) -> None: ...

    async def shutdown(self) -> None: ...


class PluginRuntime(Protocol):
    config: NyanpasuConfig

    async def submit(self, task: AgentTask) -> dict[str, Any]: ...

    async def run_now(self, task: AgentTask) -> TaskRunResult: ...

    def add_router(self, router: APIRouter, *, prefix: str = "", tags: list[str] | None = None) -> None: ...

    def add_post_process_hook(self, plugin_id: str, hook: PostProcessHook) -> None: ...


class PluginRegistry:
    def __init__(self, plugins: dict[str, NyanpasuPlugin] | None = None) -> None:
        self.plugins = plugins or {}

    def register(self, plugin: NyanpasuPlugin) -> None:
        self.plugins[plugin.id] = plugin

    def get(self, plugin_id: str) -> NyanpasuPlugin:
        try:
            return self.plugins[plugin_id]
        except KeyError as exc:
            raise ValueError(f"plugin is not registered: {plugin_id}") from exc


class PluginManager:
    def __init__(self, config: NyanpasuConfig, runtime: PluginRuntime, registry: PluginRegistry | None = None) -> None:
        self.config = config
        self.runtime = runtime
        self.registry = registry or discover_plugins()
        self.enabled: list[NyanpasuPlugin] = []

    async def setup(self) -> None:
        for plugin_id in self._enabled_plugin_ids():
            plugin = self.registry.get(plugin_id)
            raw_config = self.config.plugins.get(plugin_id, {})
            plugin_config: BaseModel | dict[str, Any]
            if plugin.config_model is not None:
                plugin_config = plugin.config_model.model_validate(raw_config)
            else:
                plugin_config = raw_config
            await plugin.setup(self.runtime, plugin_config)
            self.enabled.append(plugin)

    async def shutdown(self) -> None:
        for plugin in reversed(self.enabled):
            await plugin.shutdown()

    def _enabled_plugin_ids(self) -> tuple[str, ...]:
        if self.config.enabled_plugins:
            return self.config.enabled_plugins
        return tuple(self.config.plugins)


def discover_plugins() -> PluginRegistry:
    registry = PluginRegistry()
    for entry_point in metadata.entry_points(group="nyanpasu.plugins"):
        plugin = entry_point.load()
        if callable(plugin) and not isinstance(plugin, NyanpasuPlugin):
            plugin = plugin()
        if not isinstance(plugin, NyanpasuPlugin):
            raise TypeError(f"entry point {entry_point.name} did not return a NyanpasuPlugin")
        registry.register(plugin)
    return registry


async def maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value
