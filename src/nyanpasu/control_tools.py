from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nyanpasu.models import AgentTask

InputT = TypeVar("InputT", bound=BaseModel)


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def always_available(task: AgentTask) -> bool:
    return True


@dataclass(frozen=True)
class ToolSpec(Generic[InputT]):
    """One definition for a task action's instructions, validation and execution."""

    name: str
    description: str
    input_model: type[InputT]
    handler: Callable[[AgentTask, InputT], Awaitable[Any]]
    available: Callable[[AgentTask], bool] = always_available

    def instructions(self) -> str:
        return json.dumps(
            {
                "action": self.name,
                "description": self.description,
                "input_schema": self.input_model.model_json_schema(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    async def invoke(self, task: AgentTask, payload: dict[str, Any]) -> Any:
        if not self.available(task):
            raise ValueError(f"task action is not available: {self.name}")
        return await self.handler(task, self.input_model.model_validate(payload, strict=True))
