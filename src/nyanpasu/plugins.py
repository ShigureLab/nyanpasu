from __future__ import annotations

from collections.abc import Awaitable, Callable
from importlib import metadata
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel

from nyanpasu.models import AgentContext, AgentTask, SubtaskRequest, TaskRunResult

if TYPE_CHECKING:
    from fastapi import APIRouter

    from nyanpasu.config import NyanpasuConfig
    from nyanpasu.control_tools import ToolSpec
    from nyanpasu.memory_consolidation import MemoryPolicy

PostProcessHook = Callable[[AgentTask, TaskRunResult], Awaitable[None]]
SubtaskPreparer = Callable[[AgentTask, SubtaskRequest], Awaitable[SubtaskRequest]]
TaskPreparer = Callable[[AgentTask, tuple[AgentTask, ...], AgentContext | None], Awaitable[AgentTask]]


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

    def add_router(
        self, router: APIRouter, *, prefix: str = "", tags: list[str] | None = None, require_auth: bool = True
    ) -> None: ...

    def add_post_process_hook(self, plugin_id: str, hook: PostProcessHook) -> None: ...

    def add_task_preparer(self, plugin_id: str, preparer: TaskPreparer) -> None: ...

    def add_subtask_preparer(self, plugin_id: str, preparer: SubtaskPreparer) -> None: ...

    def add_task_control_tools(self, plugin_id: str, tools: tuple[ToolSpec[Any], ...]) -> None: ...

    def add_memory_policy(self, plugin_id: str, task_kind: str, policy: MemoryPolicy) -> None: ...


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

    def _plugin_config(self, plugin: NyanpasuPlugin) -> BaseModel | dict[str, Any]:
        raw = self.config.plugins.settings.get(plugin.id, {})
        return plugin.config_model.model_validate(raw) if plugin.config_model is not None else raw

    def register_memory_policies(self) -> None:
        """Register policies without starting plugin pollers or external integrations."""
        for plugin_id in self.config.enabled_plugin_ids:
            plugin = self.registry.get(plugin_id)
            provider: (
                dict[str, MemoryPolicy]
                | Callable[[BaseModel | dict[str, Any], NyanpasuConfig], dict[str, MemoryPolicy]]
            ) = getattr(plugin, "memory_policies", {})
            policies = provider if isinstance(provider, dict) else provider(self._plugin_config(plugin), self.config)
            for kind, policy in policies.items():
                self.runtime.add_memory_policy(plugin_id, kind, policy)

    async def setup(self) -> None:
        self.register_memory_policies()
        for plugin_id in self.config.enabled_plugin_ids:
            plugin = self.registry.get(plugin_id)
            await plugin.setup(self.runtime, self._plugin_config(plugin))
            self.enabled.append(plugin)

    async def shutdown(self) -> None:
        for plugin in reversed(self.enabled):
            await plugin.shutdown()


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
