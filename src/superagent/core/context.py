"""Context engine: give each agent only what it needs, inside a token budget.

Layers, in priority order: system → user → task → agent → tool → working memory →
long-term memory. Working memory is limited to the outputs of the task's direct
dependencies; long-term memory to entries relevant to the task. When the pack is
over budget the lowest-priority layers are compressed first, then dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..memory.manager import MemoryManager
from ..observability import get_logger
from ..utils import estimate_tokens, truncate_middle
from .dag import Task, TaskGraph

log = get_logger("context")

SYSTEM_RULES = (
    "You are one specialist agent inside Super Agent X, working on a single task of a larger plan.\n"
    "- Do exactly this task; other agents handle the rest.\n"
    "- Use only the facts provided here or produced by your tools. If something is unknown, say so.\n"
    "- Never invent file contents, numbers, sources or results.\n"
    "- Risky actions (deleting, publishing, paying, sending) are approved by a human, not by you.\n"
    "- Reply in the user's language unless the task says otherwise."
)

# Layers that may never be dropped or shortened.
_PROTECTED = {"system", "task"}


@dataclass
class ContextLayer:
    name: str
    priority: int
    text: str

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass
class ContextPack:
    layers: list[ContextLayer] = field(default_factory=list)
    budget: int = 0
    compressed: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(layer.tokens for layer in self.layers)

    def layer(self, name: str) -> str:
        return next((layer.text for layer in self.layers if layer.name == name), "")

    def render(self, *, include_system: bool = False) -> str:
        parts = []
        for layer in sorted(self.layers, key=lambda item: item.priority):
            if layer.name == "system" and not include_system:
                continue
            if layer.text.strip():
                parts.append(f"## {layer.name.replace('_', ' ').title()}\n{layer.text.strip()}")
        return "\n\n".join(parts)

    def stats(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens, "budget": self.budget, "layers": {layer.name: layer.tokens for layer in self.layers},
            "compressed": self.compressed, "dropped": self.dropped,
        }  # fmt: skip


def summarize_value(value: Any, max_chars: int = 1500) -> str:
    """Compact, loss-aware rendering of a task output for another agent's context."""
    if isinstance(value, str):
        return truncate_middle(value, max_chars)
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= max_chars:
        return text
    if isinstance(value, dict):
        slim: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str):
                slim[key] = truncate_middle(item, 300)
            elif isinstance(item, list):
                slim[key] = item[:3] + [f"…{len(item) - 3} more"] if len(item) > 3 else item
            elif isinstance(item, dict):
                slim[key] = f"{{…{len(item)} keys: {', '.join(list(item)[:6])}}}"
            else:
                slim[key] = item
        text = json.dumps(slim, ensure_ascii=False, default=str)
    return truncate_middle(text, max_chars)


class ContextManager:
    def __init__(self, memory: MemoryManager | None = None, *, default_budget: int = 6000):
        self.memory = memory
        self.default_budget = default_budget

    async def build(
        self,
        *,
        task: Task,
        graph: TaskGraph,
        command: str,
        execution_id: str,
        agent_description: str = "",
        tool_summaries: list[str] | None = None,
        resolved_input: dict[str, Any] | None = None,
        language: str | None = None,
        budget_tokens: int | None = None,
        memory_scopes: list[str] | None = None,
    ) -> ContextPack:
        budget = budget_tokens or self.default_budget
        pack = ContextPack(budget=budget)
        pack.layers.append(ContextLayer("system", 0, SYSTEM_RULES))

        user_lines = [f"Original request: {truncate_middle(command, 2000)}"]
        if language:
            user_lines.append(f"User language: {language}")
        preferences: list[str] = []
        recalled: list[str] = []
        if self.memory is not None:
            try:
                # Standing preferences always apply; everything else is recalled by relevance to this task.
                standing = await self.memory.top("preference", scopes=memory_scopes, limit=4)
                hits = await self.memory.search(f"{task.description} {command}"[:600], k=6, scopes=memory_scopes)
            except Exception:  # memory is an aid; its failure must not stop the task
                log.warning("memory lookup failed while building context", exc_info=True)
                standing, hits = [], []
            preferences = [f"- {hit.content}" for hit in standing]
            recalled = [f"- ({hit.kind}) {hit.content}" for hit in hits if hit.kind != "preference"]
            turns = self.memory.short_term(execution_id).render(400)
            if turns:
                user_lines.append(f"Recent turns:\n{turns}")
        if preferences:
            user_lines.append("Known user preferences:\n" + "\n".join(preferences[:4]))
        pack.layers.append(ContextLayer("user", 1, "\n".join(user_lines)))

        task_lines = [f"Task: {task.description}", f"Action: {task.agent}.{task.action}"]
        if resolved_input:
            task_lines.append(f"Input: {summarize_value(resolved_input, 2500)}")
        recovery = task.metadata.get("recovery") or {}
        if recovery.get("repair_hint"):
            task_lines.append(f"The previous attempt was rejected: {recovery['repair_hint']}")
        if recovery.get("debug_context"):
            task_lines.append(f"The previous attempt failed with:\n{recovery['debug_context']}")
        pack.layers.append(ContextLayer("task", 2, "\n".join(task_lines)))

        if agent_description:
            pack.layers.append(ContextLayer("agent", 3, agent_description))
        if tool_summaries:
            pack.layers.append(ContextLayer("tool", 4, "\n".join(f"- {line}" for line in tool_summaries)))

        dependency_notes = []
        for dep_id in task.dependencies:
            dep = graph.get(dep_id)
            if dep.output is not None:
                dependency_notes.append(f"[{dep.agent}.{dep.action}] {dep.description}\n{summarize_value(dep.output)}")
        if dependency_notes:
            pack.layers.append(ContextLayer("working_memory", 5, "\n\n".join(dependency_notes)))
        if recalled:
            pack.layers.append(ContextLayer("long_term_memory", 6, "\n".join(recalled[:5])))

        self._fit(pack)
        return pack

    @staticmethod
    def _fit(pack: ContextPack) -> None:
        """Compress, then drop, lowest-priority layers until the pack fits its budget."""
        for layer in sorted(pack.layers, key=lambda item: -item.priority):
            if pack.tokens <= pack.budget:
                return
            if layer.name in _PROTECTED:
                continue
            overflow = pack.tokens - pack.budget
            target_tokens = max(0, layer.tokens - overflow)
            if target_tokens >= 60:
                ratio = len(layer.text) / max(1, layer.tokens)
                layer.text = truncate_middle(layer.text, int(target_tokens * ratio))
                pack.compressed.append(layer.name)
            else:
                pack.layers.remove(layer)
                pack.dropped.append(layer.name)
