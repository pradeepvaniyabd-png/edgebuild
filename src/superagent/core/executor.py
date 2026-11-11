"""Parallel DAG executor.

Runs every READY task concurrently (bounded by a semaphore), unlocks dependents as
tasks succeed, parks tasks that need human approval, and routes failures through
the self-healing engine instead of retrying blindly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..errors import ApprovalDenied, ApprovalRequired, TaskTimeout
from ..observability import get_logger
from ..types import ApprovalStatus, TaskStatus
from ..utils import utcnow
from .approval import ApprovalManager
from .dag import GraphError, Task, TaskGraph
from .healing import SelfHealingEngine

log = get_logger("executor")


@dataclass
class TaskOutcome:
    output: dict[str, Any] = field(default_factory=dict)
    spawned: list[Task] = field(default_factory=list)


Handler = Callable[[Task, dict[str, Any]], Awaitable[TaskOutcome]]
Listener = Callable[[Task, str, dict[str, Any]], Awaitable[None]]


class ParallelExecutor:
    def __init__(
        self,
        handler: Handler,
        *,
        healing: SelfHealingEngine | None = None,
        approvals: ApprovalManager | None = None,
        max_concurrency: int = 4,
        listener: Listener | None = None,
    ):
        self.handler = handler
        self.healing = healing or SelfHealingEngine()
        self.approvals = approvals
        self.max_concurrency = max(1, max_concurrency)
        self.listener = listener
        self.peak_concurrency = 0
        self._active = 0

    async def run(self, graph: TaskGraph, cancel: asyncio.Event | None = None) -> dict[str, int]:
        cancel = cancel or asyncio.Event()
        semaphore = asyncio.Semaphore(self.max_concurrency)
        in_flight: dict[str, asyncio.Task[None]] = {}
        cancel_waiter = asyncio.create_task(cancel.wait())
        try:
            while True:
                for task in graph.refresh():
                    await self._notify(task, "task.ready" if task.status is TaskStatus.READY else "task.blocked")
                if cancel.is_set():
                    break
                for task in graph.ready():
                    if task.id not in in_flight:
                        in_flight[task.id] = asyncio.create_task(
                            self._run_task(graph, task, semaphore, cancel), name=f"task:{task.id}"
                        )
                if not in_flight:
                    break
                done, _ = await asyncio.wait(
                    {*in_flight.values(), cancel_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                for finished in done:
                    if finished is cancel_waiter:
                        continue
                    task_id = next(tid for tid, running in in_flight.items() if running is finished)
                    del in_flight[task_id]
                    crash = None if finished.cancelled() else finished.exception()
                    if crash is not None:
                        task = graph.get(task_id)
                        log.error("executor crashed while running %s", task_id, exc_info=crash)
                        task.status = TaskStatus.FAILED
                        task.error = f"internal executor error: {crash!r}"
                        task.completed_at = utcnow()
                        await self._notify(task, "task.failed", {"error": task.error})

            if cancel.is_set():
                for running in in_flight.values():
                    running.cancel()
                await asyncio.gather(*in_flight.values(), return_exceptions=True)
                for task in graph:
                    if not task.is_terminal:
                        task.status = TaskStatus.CANCELLED
                        task.completed_at = utcnow()
                        await self._notify(task, "task.cancelled")
        finally:
            cancel_waiter.cancel()
        return graph.counts()

    # ----------------------------------------------------------------- task
    async def _run_task(
        self, graph: TaskGraph, task: Task, semaphore: asyncio.Semaphore, cancel: asyncio.Event
    ) -> None:
        while True:
            error: BaseException | None = None
            outcome: TaskOutcome | None = None
            async with semaphore:
                if cancel.is_set():
                    return
                self._active += 1
                self.peak_concurrency = max(self.peak_concurrency, self._active)
                task.status = TaskStatus.RUNNING
                task.started_at = task.started_at or utcnow()
                task.error = None
                await self._notify(task, "task.started", {"attempt": task.retry_count + 1})
                recovery = task.metadata.get("recovery") or {}
                timeout = task.timeout * float(recovery.get("timeout_multiplier", 1.0))
                try:
                    resolved = graph.resolve_input(task)
                    async with asyncio.timeout(timeout):
                        outcome = await self.handler(task, resolved)
                except asyncio.CancelledError:
                    task.status = TaskStatus.CANCELLED
                    task.completed_at = utcnow()
                    raise
                except TimeoutError as exc:
                    error = TaskTimeout(str(exc) or f"task exceeded its {timeout:g}s timeout")
                except Exception as exc:
                    error = exc
                finally:
                    self._active -= 1

            if error is None and outcome is not None:
                try:
                    graph.add_many(outcome.spawned)
                except GraphError as exc:
                    error = exc
                else:
                    task.output = outcome.output
                    task.status = TaskStatus.SUCCESS
                    task.completed_at = utcnow()
                    await self._notify(task, "task.succeeded", {"spawned": len(outcome.spawned)})
                    for spawned in outcome.spawned:
                        await self._notify(spawned, "task.created", {"parent": task.id})
                    return

            if isinstance(error, ApprovalRequired):
                task.status = TaskStatus.WAITING_APPROVAL
                task.metadata["approval_id"] = error.approval_id
                await self._notify(
                    task, "task.waiting_approval",
                    {"approval_id": error.approval_id, "action": error.action, "reason": error.reason},
                )  # fmt: skip
                if self.approvals is None or not self.approvals.blocking:
                    return  # parked: the execution pauses and resumes once a human decides
                decision = await self.approvals.wait(error.approval_id, cancel)
                if decision is ApprovalStatus.APPROVED:
                    continue
                if decision is ApprovalStatus.PENDING:
                    return
                error = ApprovalDenied(f"{error.action} was denied")

            assert error is not None
            plan = self.healing.plan(task, error)
            detail = {"error": f"{type(error).__name__}: {error}", **plan.to_dict()}
            await self._notify(task, "task.healing", detail)
            if not plan.retries:
                task.status = TaskStatus.FAILED
                task.error = detail["error"]
                task.metadata["failure"] = {
                    "error_class": plan.error_class.value,
                    "reason": plan.reason,
                    "remediation": plan.remediation,
                }
                task.completed_at = utcnow()
                await self._notify(task, "task.failed", detail)
                return

            task.retry_count += 1
            task.status = TaskStatus.RETRYING
            task.metadata["recovery"] = plan.modifications
            await self._notify(task, "task.retrying", detail)
            if plan.delay > 0:
                try:
                    await asyncio.wait_for(cancel.wait(), timeout=plan.delay)
                    return  # cancelled during backoff
                except TimeoutError:
                    pass

    async def _notify(self, task: Task, event: str, data: dict[str, Any] | None = None) -> None:
        if self.listener is None:
            return
        try:
            await self.listener(task, event, data or {})
        except Exception:  # persistence/telemetry problems must not break execution
            log.warning("listener failed for %s on %s", event, task.id, exc_info=True)
