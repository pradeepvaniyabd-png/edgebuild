"""SystemAgent: reports on the platform itself (providers, tools, agents, spend)."""

from __future__ import annotations

from pydantic import BaseModel

from ..types import PermissionLevel
from .base import AgentContext, AgentResult, BaseAgent, action


class _Empty(BaseModel):
    pass


class SystemAgent(BaseAgent):
    name = "system"
    description = "Reports provider health, registered agents and tools, counts and spend."
    capabilities = ["system.status"]
    permissions = PermissionLevel.READ_ONLY

    @action("status", description="Show provider health, agents, tools, record counts and spend.", input=_Empty)
    async def status(self, ctx: AgentContext, args: _Empty) -> AgentResult:
        platform = ctx.services
        providers = [h.model_dump(mode="json") for h in platform.router.health()]
        usable = sorted({p["name"] for p in providers if p["status"] == "AVAILABLE"})
        missing = sorted(p["name"] for p in providers if p["status"] == "NOT_CONFIGURED")
        output = {
            "providers": providers,
            "available_providers": usable,
            "unconfigured_providers": missing,
            "agents": platform.agents.names(),
            "tools": platform.tools.names(),
            "counts": await ctx.repo.counts(),
            "cost": await platform.cost.summary(),
        }
        text = f"{len(usable)} provider(s) ready: {', '.join(usable) or 'none'}; {len(missing)} not configured."
        output["text"] = text
        return AgentResult(output=output, summary=text, verify=[{"type": "schema", "required": ["providers", "agents"]}])
