"""Execution event bus: persisted for replay, fanned out live to subscribers (SSE, CLI)."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from ..db.repo import Repo
from ..observability import get_logger
from ..security.secrets import mask_obj
from ..utils import utcnow

log = get_logger("events")

TERMINAL_EVENT = "execution.completed"


class EventBus:
    def __init__(self, repo: Repo | None = None):
        self.repo = repo
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    async def emit(
        self, execution_id: str, type: str, payload: dict[str, Any] | None = None, task_id: str | None = None
    ) -> dict[str, Any]:
        safe = mask_obj(payload or {})
        event: dict[str, Any] = {
            "id": 0,
            "execution_id": execution_id,
            "task_id": task_id,
            "type": type,
            "payload": safe,
            "created_at": utcnow(),
        }
        if self.repo is not None:
            try:
                event = await self.repo.add_event(execution_id, type, safe, task_id)
            except Exception:  # an event write must never take the execution down
                log.warning("could not persist event %s", type, exc_info=True)
        for queue in list(self._subscribers.get(execution_id, ())):
            queue.put_nowait(event)
        return event

    def subscribe(self, execution_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers[execution_id].add(queue)
        return queue

    def unsubscribe(self, execution_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = self._subscribers.get(execution_id)
        if subscribers:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(execution_id, None)

    async def stream(
        self, execution_id: str, after_id: int = 0, *, heartbeat: float = 15.0
    ) -> AsyncIterator[dict[str, Any] | None]:
        """Replay persisted events after ``after_id``, then follow live ones.

        Yields ``None`` as a heartbeat when idle. Stops after the terminal event.
        """
        queue = self.subscribe(execution_id)
        last_id = after_id
        try:
            if self.repo is not None:
                for event in await self.repo.list_events(execution_id, after_id=after_id):
                    last_id = max(last_id, event["id"])
                    yield event
                    if event["type"] == TERMINAL_EVENT:
                        return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                except TimeoutError:
                    yield None
                    continue
                if event["id"] and event["id"] <= last_id:
                    continue  # already delivered during replay
                last_id = max(last_id, event["id"] or last_id)
                yield event
                if event["type"] == TERMINAL_EVENT:
                    return
        finally:
            self.unsubscribe(execution_id, queue)
