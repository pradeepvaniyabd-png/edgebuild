from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..errors import ConfigError
from ..types import PermissionLevel, RiskLevel
from .registry import ToolContext, ToolRegistry, ToolSpec


class MemorySaveInput(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    kind: str = "fact"
    scope: str = "global"
    tags: list[str] = Field(default_factory=list)
    importance: float = Field(0.5, ge=0, le=1)


class MemorySearchInput(BaseModel):
    query: str = Field(min_length=1, max_length=600)
    k: int = Field(5, ge=1, le=25)
    kinds: list[str] | None = None
    scopes: list[str] | None = None


def _memory(ctx: ToolContext):  # noqa: ANN202
    if ctx.memory is None:
        raise ConfigError("memory is not available in this context")
    return ctx.memory


async def memory_save(args: MemorySaveInput, ctx: ToolContext) -> dict[str, Any]:
    saved = await _memory(ctx).save(
        args.content, kind=args.kind, scope=args.scope, tags=args.tags, importance=args.importance
    )
    return {"id": saved["id"], "kind": saved["kind"], "deduplicated": saved.get("deduplicated", False)}


async def memory_search(args: MemorySearchInput, ctx: ToolContext) -> dict[str, Any]:
    hits = await _memory(ctx).search(args.query, k=args.k, kinds=args.kinds, scopes=args.scopes)
    return {"query": args.query, "results": [hit.to_dict() for hit in hits]}


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec("memory.save", "Remember a preference, decision, fact, workflow or fix (never credentials).",
                 MemorySaveInput, memory_save, PermissionLevel.SAFE_WRITE, RiskLevel.LOW, 60)  # fmt: skip
    )
    registry.register(
        ToolSpec("memory.search", "Recall stored memories relevant to a query.", MemorySearchInput, memory_search,
                 PermissionLevel.READ_ONLY, RiskLevel.LOW, 60)  # fmt: skip
    )
