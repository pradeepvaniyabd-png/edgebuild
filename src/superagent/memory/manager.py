"""Memory: short-term (recent turns), working (per-execution scratch) and long-term (persisted, searchable).

Long-term memory refuses to store credentials. That is enforced here, at the single
write path, not left to callers.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..config import Settings
from ..db.repo import Repo
from ..errors import SuperAgentError
from ..observability import get_logger
from ..providers.services import AI
from ..providers.vendors.hashing import HashingEmbedding, cosine, embed_text
from ..security.secrets import find_secrets, redact_pii
from ..types import ErrorClass, ProviderKind
from ..utils import estimate_tokens, truncate_middle

log = get_logger("memory")

_HASHING = HashingEmbedding()
_HASHING_ID = f"{_HASHING.name}:{_HASHING.model}"
_DUPLICATE_THRESHOLD = 0.97


class MemoryKind(StrEnum):
    PREFERENCE = "preference"
    DECISION = "decision"
    WORKFLOW = "workflow"
    FACT = "fact"
    CONFIG = "config"
    FIX = "fix"


class MemoryRejected(SuperAgentError):
    error_class = ErrorClass.VALIDATION


@dataclass
class MemoryHit:
    id: str
    kind: str
    scope: str
    content: str
    score: float
    tags: list[str]
    importance: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "scope": self.scope, "content": self.content,
            "score": round(self.score, 4), "tags": self.tags, "importance": self.importance,
        }  # fmt: skip


class ShortTermMemory:
    """The last few conversational turns of one execution."""

    def __init__(self, max_turns: int = 20):
        self._turns: deque[tuple[str, str]] = deque(maxlen=max_turns)

    def add(self, role: str, content: str) -> None:
        self._turns.append((role, content))

    def recent(self, count: int = 6) -> list[tuple[str, str]]:
        return list(self._turns)[-count:]

    def render(self, budget_tokens: int = 600) -> str:
        lines: list[str] = []
        used = 0
        for role, content in reversed(self._turns):
            line = f"{role}: {truncate_middle(content, 800)}"
            used += estimate_tokens(line)
            if used > budget_tokens:
                break
            lines.append(line)
        return "\n".join(reversed(lines))


class WorkingMemory:
    """Scratch space for one execution: what each finished task produced, in brief."""

    def __init__(self) -> None:
        self._items: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> None:
        self._items[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._items.get(key, default)

    def items(self) -> dict[str, Any]:
        return dict(self._items)

    def render(self, keys: list[str] | None = None, max_chars_each: int = 1200) -> str:
        chosen = keys if keys is not None else list(self._items)
        return "\n".join(
            f"[{key}] {truncate_middle(str(self._items[key]), max_chars_each)}" for key in chosen if key in self._items
        )


class LongTermMemory:
    def __init__(self, repo: Repo, settings: Settings, ai: AI | None = None):
        self.repo = repo
        self.settings = settings
        self.ai = ai

    async def _embed(self, texts: list[str]) -> tuple[list[list[float]], str]:
        if self.ai is not None and self.ai.router.available(ProviderKind.EMBEDDING):
            try:
                return await self.ai.embed(texts)
            except SuperAgentError:
                log.warning("embedding provider failed; falling back to built-in hashing", exc_info=True)
        return [embed_text(text) for text in texts], _HASHING_ID

    async def save(
        self,
        content: str,
        *,
        kind: MemoryKind | str = MemoryKind.FACT,
        scope: str = "global",
        tags: list[str] | None = None,
        importance: float = 0.5,
    ) -> dict[str, Any]:
        text = content.strip()
        if not text:
            raise MemoryRejected("refusing to store an empty memory")
        found = find_secrets(text)
        if found:
            raise MemoryRejected(
                f"refusing to store what looks like a credential ({', '.join(found)})",
                hint="keep secrets in the environment or a secret manager, never in memory",
            )
        if self.settings.redact_pii:
            text = redact_pii(text)
        kind_value = MemoryKind(kind).value
        (vector,), model = await self._embed([text])

        for existing in await self.repo.list_memories(kind=kind_value, scopes=[scope]):
            if existing.get("embedding_model") == model and existing.get("embedding"):
                if cosine(vector, existing["embedding"]) >= _DUPLICATE_THRESHOLD:
                    await self.repo.update_memory(
                        existing["id"], content=text, importance=max(importance, existing["importance"]),
                        tags=sorted({*existing.get("tags", []), *(tags or [])}), embedding=vector,
                    )  # fmt: skip
                    return {**existing, "content": text, "deduplicated": True}
        saved = await self.repo.add_memory(
            kind=kind_value, scope=scope, content=text, tags=sorted(set(tags or [])),
            embedding=vector, embedding_model=model, importance=max(0.0, min(1.0, importance)),
        )  # fmt: skip
        return {**saved, "deduplicated": False}

    async def search(
        self,
        query: str,
        *,
        k: int = 5,
        kinds: list[str] | None = None,
        scopes: list[str] | None = None,
        min_score: float = 0.12,
    ) -> list[MemoryHit]:
        if not query.strip():
            return []
        rows = await self.repo.list_memories(scopes=scopes or ["global"])
        if kinds:
            rows = [row for row in rows if row["kind"] in kinds]
        if not rows:
            return []
        (query_vector,), model = await self._embed([query])
        hashed_query: list[float] | None = None
        hits: list[MemoryHit] = []
        for row in rows:
            if row.get("embedding_model") == model and row.get("embedding"):
                similarity = cosine(query_vector, row["embedding"])
            else:  # stored with a different embedder: compare lexically instead of mixing vector spaces
                hashed_query = hashed_query or embed_text(query)
                similarity = cosine(hashed_query, embed_text(row["content"]))
            score = similarity * (0.85 + 0.15 * float(row.get("importance") or 0.5))
            if score >= min_score:
                hits.append(MemoryHit(row["id"], row["kind"], row["scope"], row["content"], score,
                                      list(row.get("tags") or []), float(row.get("importance") or 0.5)))  # fmt: skip
        hits.sort(key=lambda hit: hit.score, reverse=True)
        top = hits[:k]
        await self.repo.touch_memories([hit.id for hit in top])
        return top

    async def top(self, kind: MemoryKind | str, *, scopes: list[str] | None = None, limit: int = 4) -> list[MemoryHit]:
        """Most important memories of one kind, regardless of query (used for standing preferences)."""
        rows = await self.repo.list_memories(kind=MemoryKind(kind).value, scopes=scopes or ["global"])
        rows.sort(key=lambda row: float(row.get("importance") or 0.0), reverse=True)
        return [
            MemoryHit(row["id"], row["kind"], row["scope"], row["content"], 1.0, list(row.get("tags") or []),
                      float(row.get("importance") or 0.5))
            for row in rows[:limit]
        ]  # fmt: skip

    async def update(self, memory_id: str, *, content: str | None = None, importance: float | None = None,
                     tags: list[str] | None = None) -> dict[str, Any] | None:  # fmt: skip
        fields: dict[str, Any] = {}
        if content is not None:
            if find_secrets(content):
                raise MemoryRejected("refusing to store what looks like a credential")
            text = redact_pii(content.strip()) if self.settings.redact_pii else content.strip()
            (vector,), model = await self._embed([text])
            fields.update(content=text, embedding=vector, embedding_model=model)
        if importance is not None:
            fields["importance"] = max(0.0, min(1.0, importance))
        if tags is not None:
            fields["tags"] = sorted(set(tags))
        if fields:
            await self.repo.update_memory(memory_id, **fields)
        return await self.repo.get_memory(memory_id)

    async def delete(self, memory_id: str) -> bool:
        return await self.repo.delete_memory(memory_id)

    async def summarize(self, scope: str = "global") -> str:
        rows = await self.repo.list_memories(scopes=[scope])
        if not rows:
            return "No memories stored."
        grouped: dict[str, list[str]] = {}
        for row in sorted(rows, key=lambda r: -float(r.get("importance") or 0)):
            grouped.setdefault(row["kind"], []).append(row["content"])
        lines = []
        for kind, items in grouped.items():
            lines.append(f"{kind} ({len(items)}):")
            lines += [f"  - {truncate_middle(item, 200)}" for item in items[:8]]
        return "\n".join(lines)


class MemoryManager:
    def __init__(self, repo: Repo, settings: Settings, ai: AI | None = None):
        self.long_term = LongTermMemory(repo, settings, ai)
        self._short: dict[str, ShortTermMemory] = {}
        self._working: dict[str, WorkingMemory] = {}

    def short_term(self, execution_id: str) -> ShortTermMemory:
        return self._short.setdefault(execution_id, ShortTermMemory())

    def working(self, execution_id: str) -> WorkingMemory:
        return self._working.setdefault(execution_id, WorkingMemory())

    def forget_execution(self, execution_id: str) -> None:
        self._short.pop(execution_id, None)
        self._working.pop(execution_id, None)

    async def save(self, content: str, **kwargs: Any) -> dict[str, Any]:
        return await self.long_term.save(content, **kwargs)

    async def search(self, query: str, **kwargs: Any) -> list[MemoryHit]:
        return await self.long_term.search(query, **kwargs)

    async def top(self, kind: MemoryKind | str, **kwargs: Any) -> list[MemoryHit]:
        return await self.long_term.top(kind, **kwargs)

    async def update(self, memory_id: str, **kwargs: Any) -> dict[str, Any] | None:
        return await self.long_term.update(memory_id, **kwargs)

    async def delete(self, memory_id: str) -> bool:
        return await self.long_term.delete(memory_id)

    async def summarize(self, scope: str = "global") -> str:
        return await self.long_term.summarize(scope)
