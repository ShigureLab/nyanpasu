from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ExecutionOverride(BaseModel):
    """Sparse, backend-aware execution preferences supplied at one boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    backend: str | None = None
    model: str | None = None
    reasoning: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _compact_target(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if value.count("/") > 1 or value.count(":") > 1:
            raise ValueError(
                "compact execution target is backend[/model][:reasoning]; use an object for model IDs containing separators"
            )
        selection, separator, reasoning = value.partition(":")
        backend, slash, model = selection.partition("/")
        result = {"backend": backend}
        if slash:
            result["model"] = model
        if separator:
            result["reasoning"] = reasoning
        return result

    @field_validator("backend", "model", "reasoning")
    @classmethod
    def _nonempty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or "\0" in value:
            raise ValueError("execution settings must be nonempty and contain no NUL")
        return value


class ExecutionTarget(BaseModel):
    """Resolved execution intent, frozen when a task is admitted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: str
    driver: Literal["codex", "claude-code"]
    model: str | None = None
    reasoning: str | None = None
    turn_timeout_seconds: int = Field(gt=0)
    sources: dict[str, str] = Field(default_factory=dict)
