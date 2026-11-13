"""Agent contract.

An agent is a set of named *actions*. Each action declares its input model, so the
planner knows what it can ask for and the orchestrator can validate a task before
running it. Agents reach the outside world only through tools and the AI facade.
"""

from __future__ import annotations

import json
from abc import ABC
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field, ValidationError

from ..config import Settings
from ..core.context import ContextPack
from ..core.dag import Task
from ..errors import ValidationFailed
from ..tools.registry import ToolContext, ToolRegistry
from ..types import PermissionLevel, ProviderKind

if TYPE_CHECKING:
    from ..core.events import EventBus
    from ..db.repo import Repo
    from ..memory.manager import MemoryManager
    from ..providers.services import AI


class NoInput(BaseModel):
    pass


class AgentResult(BaseModel):
    output: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    verify: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    spawn: list[Task] = Field(default_factory=list)


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    method: str
    needs_llm: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "needs_llm": self.needs_llm,
            "input_schema": self.input_model.model_json_schema(),
        }


def action(
    name: str, *, description: str, input: type[BaseModel] = NoInput, needs_llm: bool = False
) -> Callable[[Callable[..., Awaitable[AgentResult]]], Callable[..., Awaitable[AgentResult]]]:
    def mark(method: Callable[..., Awaitable[AgentResult]]) -> Callable[..., Awaitable[AgentResult]]:
        method._action_spec = ActionSpec(name, description, input, method.__name__, needs_llm)  # type: ignore[attr-defined]
        return method

    return mark


class BoundTools:
    """The tool registry seen through one agent's permission level."""

    def __init__(self, registry: ToolRegistry, context: ToolContext):
        self._registry = registry
        self._context = context

    async def call(self, name: str, **args: Any) -> dict[str, Any]:
        return await self._registry.call(name, args, self._context)

    @property
    def context(self) -> ToolContext:
        return self._context


@dataclass
class AgentContext:
    settings: Settings
    execution_id: str
    task: Task
    input: dict[str, Any]
    tools: BoundTools
    ai: AI
    memory: MemoryManager
    repo: Repo
    events: EventBus
    context: ContextPack
    command: str = ""
    language: str = "en"
    recovery: dict[str, Any] = field(default_factory=dict)
    project_id: str | None = None
    services: Any = None

    @property
    def workspace(self) -> Path:
        return self.settings.workspace

    @property
    def has_llm(self) -> bool:
        return self.ai.has(ProviderKind.LLM)

    def out_dir(self, *parts: str) -> Path:
        path = self.settings.outputs_dir.joinpath(self.execution_id, *parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def emit(self, type: str, payload: dict[str, Any] | None = None) -> None:
        await self.events.emit(self.execution_id, type, payload or {}, self.task.id)

    def prompt(self, instruction: str) -> str:
        """The task context followed by an instruction, ready to send to a model."""
        return f"{self.context.render()}\n\n## Instruction\n{instruction}"

    @property
    def system_prompt(self) -> str:
        return self.context.layer("system")


class BaseAgent(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    capabilities: ClassVar[list[str]] = []
    permissions: ClassVar[PermissionLevel] = PermissionLevel.READ_ONLY
    actions: ClassVar[dict[str, ActionSpec]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        found: dict[str, ActionSpec] = {}
        for klass in reversed(cls.__mro__):
            for attribute in vars(klass).values():
                spec = getattr(attribute, "_action_spec", None)
                if isinstance(spec, ActionSpec):
                    found[spec.name] = spec
        cls.actions = found

    # ----------------------------------------------------------------- schema
    @property
    def input_schema(self) -> dict[str, Any]:
        return {name: spec.input_model.model_json_schema() for name, spec in self.actions.items()}

    @property
    def output_schema(self) -> dict[str, Any]:
        return AgentResult.model_json_schema()

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "capabilities": self.capabilities,
            "permissions": self.permissions.name,
            "actions": {name: spec.describe() for name, spec in self.actions.items()},
        }

    # ------------------------------------------------------------- validation
    def validate_input(self, action_name: str, data: dict[str, Any]) -> BaseModel:
        spec = self.actions.get(action_name)
        if spec is None:
            known = ", ".join(sorted(self.actions)) or "none"
            raise ValidationFailed(f"agent '{self.name}' has no action '{action_name}' (available: {known})")
        try:
            return spec.input_model.model_validate(data)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()[:5])
            raise ValidationFailed(f"invalid input for {self.name}.{action_name}: {problems}") from exc

    def validate_output(self, action_name: str, result: AgentResult) -> None:
        if not isinstance(result, AgentResult):
            raise ValidationFailed(f"{self.name}.{action_name} did not return an AgentResult")
        try:
            json.dumps(result.output, default=str)
        except (TypeError, ValueError) as exc:
            raise ValidationFailed(f"{self.name}.{action_name} returned output that cannot be serialised: {exc}") from exc

    # ---------------------------------------------------------------- execute
    async def execute(self, ctx: AgentContext) -> AgentResult:
        args = self.validate_input(ctx.task.action, ctx.input)
        result: AgentResult = await getattr(self, self.actions[ctx.task.action].method)(ctx, args)
        self.validate_output(ctx.task.action, result)
        return result


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> BaseAgent:
        if agent.name in self._agents:
            raise ValueError(f"agent '{agent.name}' is already registered")
        self._agents[agent.name] = agent
        return agent

    def get(self, name: str) -> BaseAgent:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise ValidationFailed(f"unknown agent '{name}' (known: {', '.join(sorted(self._agents))})") from exc

    def __contains__(self, name: object) -> bool:
        return name in self._agents

    def names(self) -> list[str]:
        return sorted(self._agents)

    def describe(self) -> list[dict[str, Any]]:
        return [self._agents[name].describe() for name in self.names()]

    def catalog(self) -> dict[str, dict[str, str]]:
        """agent → action → one-line description. What the planner is allowed to use."""
        return {
            name: {action_name: spec.description for action_name, spec in agent.actions.items()}
            for name, agent in sorted(self._agents.items())
        }
