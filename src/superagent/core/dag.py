"""Dependency-aware task graph.

Tasks reference each other's outputs with ``${task_id.path.into.output}`` (or
``${task_id}`` for the whole output); the reference is resolved when the task is
dispatched, after its dependencies succeeded.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ..types import TERMINAL_TASK_STATUSES, TaskStatus
from ..utils import deep_get, new_id, utcnow

_REF = re.compile(r"\$\{([A-Za-z0-9_\-]+)(?:\.([^}]+))?\}")
_UNSATISFIABLE = {TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}


class GraphError(ValueError):
    """Invalid graph structure: unknown dependency, duplicate id, cycle or bad reference."""


class Task(BaseModel):
    id: str = Field(default_factory=lambda: new_id("tsk"))
    parent_id: str | None = None
    description: str
    agent: str
    action: str
    dependencies: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 5
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    retry_count: int = 0
    max_retries: int = 2
    timeout: float = 900.0
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATUSES

    @property
    def optional(self) -> bool:
        """Optional tasks never block their dependents; a failed one resolves to ``None``."""
        return bool(self.metadata.get("optional"))


class TaskGraph:
    def __init__(self, tasks: Iterable[Task] = ()):
        self._tasks: dict[str, Task] = {}
        self.add_many(tasks)

    # ------------------------------------------------------------ container
    def __len__(self) -> int:
        return len(self._tasks)

    def __iter__(self) -> Iterator[Task]:
        return iter(self._tasks.values())

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._tasks

    @property
    def tasks(self) -> list[Task]:
        return list(self._tasks.values())

    def get(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise GraphError(f"unknown task '{task_id}'") from exc

    def add(self, task: Task) -> Task:
        self.add_many([task])
        return task

    def add_many(self, tasks: Iterable[Task]) -> None:
        """Add tasks atomically: on any structural error the graph is left unchanged."""
        new = list(tasks)
        if not new:
            return
        snapshot = dict(self._tasks)
        try:
            for task in new:
                if task.id in self._tasks:
                    raise GraphError(f"duplicate task id '{task.id}'")
                self._tasks[task.id] = task
            self.validate()
        except GraphError:
            self._tasks = snapshot
            raise

    # ----------------------------------------------------------- structure
    def validate(self) -> None:
        for task in self._tasks.values():
            for dep in task.dependencies:
                if dep == task.id:
                    raise GraphError(f"task '{task.id}' depends on itself")
                if dep not in self._tasks:
                    raise GraphError(f"task '{task.id}' depends on unknown task '{dep}'")
        self.topological_order()

    def topological_order(self) -> list[Task]:
        indegree = {tid: len(set(task.dependencies)) for tid, task in self._tasks.items()}
        dependents: dict[str, list[str]] = {tid: [] for tid in self._tasks}
        for tid, task in self._tasks.items():
            for dep in set(task.dependencies):
                dependents[dep].append(tid)
        queue = deque(tid for tid, degree in indegree.items() if degree == 0)
        order: list[Task] = []
        while queue:
            tid = queue.popleft()
            order.append(self._tasks[tid])
            for child in dependents[tid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(order) != len(self._tasks):
            cyclic = sorted(tid for tid, degree in indegree.items() if degree > 0)
            raise GraphError(f"dependency cycle among tasks: {', '.join(cyclic)}")
        return order

    def levels(self) -> list[list[Task]]:
        """Group tasks into waves; every task in a wave can run in parallel."""
        depth: dict[str, int] = {}
        for task in self.topological_order():
            depth[task.id] = 1 + max((depth[d] for d in task.dependencies), default=-1)
        waves: list[list[Task]] = [[] for _ in range(max(depth.values(), default=-1) + 1)]
        for task in self._tasks.values():
            waves[depth[task.id]].append(task)
        return waves

    def dependents(self, task_id: str) -> list[Task]:
        return [task for task in self._tasks.values() if task_id in task.dependencies]

    # --------------------------------------------------------------- state
    def refresh(self) -> list[Task]:
        """Promote PENDING tasks to READY or BLOCKED. Returns the tasks whose status changed."""
        changed: list[Task] = []
        progressed = True
        while progressed:
            progressed = False
            for task in self._tasks.values():
                if task.status is not TaskStatus.PENDING:
                    continue
                deps = [self._tasks[dep] for dep in task.dependencies]
                blocker = next((d for d in deps if d.status in _UNSATISFIABLE and not d.optional), None)
                if blocker is not None:
                    task.status = TaskStatus.BLOCKED
                    task.error = f"blocked: dependency {blocker.id} ({blocker.action}) is {blocker.status.value}"
                    task.completed_at = utcnow()
                elif all(_satisfied(dep) for dep in deps):
                    task.status = TaskStatus.READY
                else:
                    continue
                changed.append(task)
                progressed = True
        return changed

    def ready(self) -> list[Task]:
        ready = [task for task in self._tasks.values() if task.status is TaskStatus.READY]
        return sorted(ready, key=lambda task: (task.priority, task.created_at))

    def is_finished(self) -> bool:
        return all(task.is_terminal for task in self._tasks.values())

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self._tasks.values():
            counts[task.status.value] = counts.get(task.status.value, 0) + 1
        return counts

    def reset_for_resume(self) -> list[Task]:
        """Requeue everything that did not succeed, keeping completed work."""
        reset: list[Task] = []
        for task in self._tasks.values():
            if task.status is TaskStatus.SUCCESS:
                continue
            task.status = TaskStatus.PENDING
            task.error = None
            task.retry_count = 0
            task.completed_at = None
            task.metadata.pop("recovery", None)
            task.metadata.pop("recovery_signatures", None)
            reset.append(task)
        return reset

    # ---------------------------------------------------------- references
    def resolve_input(self, task: Task) -> dict[str, Any]:
        return self._resolve(task.input, task)

    def _resolve(self, value: Any, task: Task) -> Any:
        if isinstance(value, dict):
            return {key: self._resolve(item, task) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve(item, task) for item in value]
        if not isinstance(value, str) or "${" not in value:
            return value
        whole = _REF.fullmatch(value)
        if whole:
            return self._lookup(whole.group(1), whole.group(2), task)
        return _REF.sub(lambda m: _as_text(self._lookup(m.group(1), m.group(2), task)), value)

    def _lookup(self, ref_id: str, path: str | None, task: Task) -> Any:
        if ref_id not in self._tasks:
            raise GraphError(f"task '{task.id}' references unknown task '{ref_id}'")
        source = self._tasks[ref_id]
        if ref_id not in task.dependencies:
            raise GraphError(f"task '{task.id}' references '{ref_id}' without depending on it")
        if source.status is not TaskStatus.SUCCESS or source.output is None:
            if source.optional:
                return None
            raise GraphError(f"task '{task.id}' references '{ref_id}', which has no output yet")
        if path is None:
            return source.output
        try:
            return deep_get(source.output, path)
        except KeyError:
            if source.optional:
                return None
            raise GraphError(f"output of '{ref_id}' has no field '{path}' (needed by '{task.id}')") from None

    # ----------------------------------------------------------- transport
    def to_list(self) -> list[dict[str, Any]]:
        return [task.model_dump(mode="json") for task in self._tasks.values()]

    @classmethod
    def from_rows(cls, rows: Iterable[dict[str, Any]]) -> TaskGraph:
        fields = set(Task.model_fields)
        return cls(Task(**{k: v for k, v in row.items() if k in fields}) for row in rows)


def _satisfied(dep: Task) -> bool:
    return dep.status is TaskStatus.SUCCESS or (dep.optional and dep.is_terminal)


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)
