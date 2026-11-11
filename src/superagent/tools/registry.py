"""Tool registry: the only doorway between agents and the machine.

Every call is validated, permission-checked against the calling agent, gated by
human approval where the tool or its arguments are risky, time-limited and audited.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..errors import ApprovalDenied, PermissionDeniedError, SuperAgentError, TaskTimeout, ToolError, ValidationFailed
from ..observability import get_logger, log_context
from ..security import CommandPolicy, PathSandbox
from ..security.secrets import mask
from ..types import PermissionLevel, RiskLevel

if TYPE_CHECKING:
    from ..core.approval import ApprovalManager
    from ..core.events import EventBus
    from ..db.repo import Repo
    from ..memory.manager import MemoryManager
    from ..providers.services import AI

log = get_logger("tools")


@dataclass
class ToolContext:
    settings: Settings
    sandbox: PathSandbox
    policy: CommandPolicy
    approvals: ApprovalManager | None = None
    events: EventBus | None = None
    ai: AI | None = None
    repo: Repo | None = None
    memory: MemoryManager | None = None
    execution_id: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    agent: str = "system"
    permission: PermissionLevel = PermissionLevel.READ_ONLY


@dataclass(frozen=True)
class ApprovalNeed:
    category: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Any, ToolContext], Awaitable[dict[str, Any]]]
Guard = Callable[[Any, ToolContext], ApprovalNeed | None]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Handler
    permission_level: PermissionLevel = PermissionLevel.READ_ONLY
    risk_level: RiskLevel = RiskLevel.LOW
    timeout: float = 60.0
    enabled: bool = True
    output_model: type[BaseModel] | None = None
    guard: Guard | None = None  # may raise SecurityError, or return an ApprovalNeed

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema() if self.output_model else {"type": "object"},
            "permission_level": self.permission_level.name,
            "risk_level": self.risk_level.name,
            "timeout": self.timeout,
            "enabled": self.enabled,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"tool '{spec.name}' is already registered")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(f"unknown tool '{name}'") from exc

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict[str, Any]]:
        return [self._tools[name].describe() for name in self.names()]

    async def call(self, name: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        spec = self.get(name)
        if not spec.enabled:
            raise ToolError(f"tool '{name}' is disabled")
        if ctx.permission < spec.permission_level:
            raise PermissionDeniedError(
                f"agent '{ctx.agent}' ({ctx.permission.name}) may not use '{name}' (needs {spec.permission_level.name})"
            )
        try:
            parsed = spec.input_model.model_validate(args)
        except ValidationError as exc:
            raise ValidationFailed(f"invalid arguments for '{name}': {_brief(exc)}") from exc

        need = spec.guard(parsed, ctx) if spec.guard else None
        if need is None and spec.permission_level is PermissionLevel.EXTERNAL_ACTION:
            need = ApprovalNeed("external_action", f"'{name}' acts outside this machine", parsed.model_dump(mode="json"))
        if need is not None:
            if ctx.approvals is None:
                raise ApprovalDenied(f"'{name}' needs approval but no approval channel is available")
            await ctx.approvals.ensure(
                action=name, category=need.category, reason=need.reason, details=need.details,
                risk=spec.risk_level, execution_id=ctx.execution_id, task_id=ctx.task_id,
            )  # fmt: skip

        started = time.perf_counter()
        ok = False
        try:
            with log_context(tool=name):
                async with asyncio.timeout(spec.timeout):
                    result = await spec.handler(parsed, ctx)
            if spec.output_model is not None:
                result = spec.output_model.model_validate(result).model_dump(mode="json")
            ok = True
            return result
        except TimeoutError as exc:
            raise TaskTimeout(f"tool '{name}' exceeded its {spec.timeout:g}s limit") from exc
        except SuperAgentError:
            raise
        except ValidationError as exc:
            raise ToolError(f"tool '{name}' returned an invalid result: {_brief(exc)}") from exc
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise ToolError(f"tool '{name}' failed: {type(exc).__name__}: {mask(str(exc))}") from exc
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000)
            log.info("tool %s %s in %dms", name, "ok" if ok else "failed", duration_ms,
                     extra={"data": {"event": "tool.called", "ok": ok, "duration_ms": duration_ms}})  # fmt: skip
            if ctx.events is not None and ctx.execution_id:
                await ctx.events.emit(
                    ctx.execution_id, "tool.called",
                    {"tool": name, "agent": ctx.agent, "ok": ok, "duration_ms": duration_ms}, ctx.task_id,
                )  # fmt: skip


def _brief(error: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in error.errors()[:4])
