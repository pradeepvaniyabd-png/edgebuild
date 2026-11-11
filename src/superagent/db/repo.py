"""Persistence API. Services talk to this, never to SQLAlchemy sessions directly."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import DeclarativeBase

from ..types import ApprovalStatus, ExecutionStatus
from ..utils import new_id, utcnow
from .models import (
    Approval,
    Asset,
    CacheEntry,
    CostRecord,
    CreativeProject,
    Execution,
    Generation,
    Memory,
    ScheduledJob,
    TaskEvent,
    TaskRow,
)
from .session import Database


def row_to_dict(row: DeclarativeBase | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for attr in row.__mapper__.column_attrs:
        value = getattr(row, attr.key)
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)  # SQLite drops tzinfo; everything is stored as UTC
        out["metadata" if attr.key == "meta" else attr.key] = value
    return out


class Repo:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------ generic
    async def _insert(self, model: type[DeclarativeBase], **fields: Any) -> dict[str, Any]:
        async with self.db.write() as session:
            row = model(**fields)
            session.add(row)
            await session.flush()
            return row_to_dict(row) or {}

    async def _update(self, model: type[DeclarativeBase], pk: Any, **fields: Any) -> None:
        if not fields:
            return
        pk_col = next(iter(model.__table__.primary_key.columns))
        attr_key = model.__mapper__.get_property_by_column(pk_col).key
        async with self.db.write() as session:
            await session.execute(update(model).where(getattr(model, attr_key) == pk).values(**fields))

    async def _get(self, model: type[DeclarativeBase], pk: Any) -> dict[str, Any] | None:
        async with self.db.session() as session:
            return row_to_dict(await session.get(model, pk))

    async def _list(
        self, model: type[DeclarativeBase], *where: Any, order_by: Any = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(model).where(*where)
        if order_by is not None:
            stmt = stmt.order_by(*order_by) if isinstance(order_by, tuple | list) else stmt.order_by(order_by)
        if limit:
            stmt = stmt.limit(limit)
        async with self.db.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [d for d in (row_to_dict(row) for row in rows) if d is not None]

    # --------------------------------------------------------- executions
    async def create_execution(
        self, *, id: str, command: str, source: str = "cli", project_id: str | None = None
    ) -> dict[str, Any]:
        return await self._insert(
            Execution, id=id, command=command, source=source, project_id=project_id,
            status=ExecutionStatus.PLANNING.value, created_at=utcnow(),
        )  # fmt: skip

    async def update_execution(self, id: str, **fields: Any) -> None:
        await self._update(Execution, id, **fields)

    async def get_execution(self, id: str) -> dict[str, Any] | None:
        return await self._get(Execution, id)

    async def list_executions(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        where = [Execution.status == status] if status else []
        return await self._list(Execution, *where, order_by=Execution.created_at.desc(), limit=limit)

    async def mark_interrupted(self) -> int:
        """Executions left RUNNING by a dead process become INTERRUPTED (resumable)."""
        active = [ExecutionStatus.RUNNING.value, ExecutionStatus.PLANNING.value]
        async with self.db.write() as session:
            result = await session.execute(
                update(Execution).where(Execution.status.in_(active)).values(status=ExecutionStatus.INTERRUPTED.value)
            )
            return result.rowcount or 0

    # -------------------------------------------------------------- tasks
    async def save_tasks(self, execution_id: str, tasks: Iterable[Any], start_position: int = 0) -> None:
        async with self.db.write() as session:
            for offset, task in enumerate(tasks):
                await session.merge(_task_row(execution_id, task, start_position + offset))

    async def save_task(self, execution_id: str, task: Any) -> None:
        data = _task_fields(task)
        async with self.db.write() as session:
            result = await session.execute(update(TaskRow).where(TaskRow.id == task.id).values(**data))
            if not result.rowcount:
                session.add(TaskRow(id=task.id, execution_id=execution_id, position=0, **data))

    async def load_tasks(self, execution_id: str) -> list[dict[str, Any]]:
        return await self._list(
            TaskRow, TaskRow.execution_id == execution_id, order_by=(TaskRow.position, TaskRow.created_at)
        )

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await self._get(TaskRow, task_id)

    # ------------------------------------------------------------- events
    async def add_event(
        self, execution_id: str, type: str, payload: dict[str, Any], task_id: str | None = None
    ) -> dict[str, Any]:
        return await self._insert(
            TaskEvent, execution_id=execution_id, task_id=task_id, type=type, payload=payload, created_at=utcnow()
        )

    async def list_events(self, execution_id: str, after_id: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        return await self._list(
            TaskEvent, TaskEvent.execution_id == execution_id, TaskEvent.id > after_id,
            order_by=TaskEvent.id, limit=limit,
        )  # fmt: skip

    # ---------------------------------------------------------- approvals
    async def create_approval(self, **fields: Any) -> dict[str, Any]:
        fields.setdefault("id", new_id("apr"))
        fields.setdefault("status", ApprovalStatus.PENDING.value)
        fields.setdefault("requested_at", utcnow())
        return await self._insert(Approval, **fields)

    async def get_approval(self, id: str) -> dict[str, Any] | None:
        return await self._get(Approval, id)

    async def find_approval(self, fingerprint: str, execution_id: str | None) -> dict[str, Any] | None:
        rows = await self._list(
            Approval, Approval.fingerprint == fingerprint, Approval.execution_id == execution_id,
            order_by=Approval.requested_at.desc(), limit=1,
        )  # fmt: skip
        return rows[0] if rows else None

    async def decide_approval(
        self, id: str, status: ApprovalStatus, decided_by: str, scope: str = "once"
    ) -> dict[str, Any] | None:
        async with self.db.write() as session:
            row = await session.get(Approval, id)
            if row is None or row.status != ApprovalStatus.PENDING.value:
                return row_to_dict(row)
            row.status = status.value
            row.decided_by = decided_by
            row.decided_at = utcnow()
            row.scope = scope
            await session.flush()
            return row_to_dict(row)

    async def list_approvals(
        self, status: str | None = None, execution_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        where = []
        if status:
            where.append(Approval.status == status)
        if execution_id:
            where.append(Approval.execution_id == execution_id)
        return await self._list(Approval, *where, order_by=Approval.requested_at.desc(), limit=limit)

    async def category_approved_for_execution(self, execution_id: str, category: str) -> bool:
        rows = await self._list(
            Approval, Approval.execution_id == execution_id, Approval.category == category,
            Approval.status == ApprovalStatus.APPROVED.value, Approval.scope == "execution", limit=1,
        )  # fmt: skip
        return bool(rows)

    # ------------------------------------------------------------- memory
    async def add_memory(self, **fields: Any) -> dict[str, Any]:
        fields.setdefault("id", new_id("mem"))
        now = utcnow()
        fields.setdefault("created_at", now)
        fields.setdefault("updated_at", now)
        return await self._insert(Memory, **fields)

    async def update_memory(self, id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        await self._update(Memory, id, **fields)

    async def get_memory(self, id: str) -> dict[str, Any] | None:
        return await self._get(Memory, id)

    async def delete_memory(self, id: str) -> bool:
        async with self.db.write() as session:
            result = await session.execute(delete(Memory).where(Memory.id == id))
            return bool(result.rowcount)

    async def list_memories(
        self, kind: str | None = None, scopes: list[str] | None = None, limit: int = 2000
    ) -> list[dict[str, Any]]:
        where = []
        if kind:
            where.append(Memory.kind == kind)
        if scopes:
            where.append(Memory.scope.in_(scopes))
        return await self._list(Memory, *where, order_by=Memory.created_at.desc(), limit=limit)

    async def touch_memories(self, ids: list[str]) -> None:
        if not ids:
            return
        async with self.db.write() as session:
            await session.execute(
                update(Memory).where(Memory.id.in_(ids))
                .values(access_count=Memory.access_count + 1, last_accessed_at=utcnow())
            )  # fmt: skip

    # ----------------------------------------------------------- creative
    async def create_project(self, **fields: Any) -> dict[str, Any]:
        now = utcnow()
        fields.setdefault("created_at", now)
        fields.setdefault("updated_at", now)
        return await self._insert(CreativeProject, **fields)

    async def update_project(self, id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        await self._update(CreativeProject, id, **fields)

    async def get_project(self, id: str) -> dict[str, Any] | None:
        return await self._get(CreativeProject, id)

    async def list_projects(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self._list(CreativeProject, order_by=CreativeProject.created_at.desc(), limit=limit)

    async def create_generation(self, **fields: Any) -> dict[str, Any]:
        fields.setdefault("id", new_id("gen"))
        fields.setdefault("created_at", utcnow())
        return await self._insert(Generation, **fields)

    async def update_generation(self, id: str, **fields: Any) -> None:
        await self._update(Generation, id, **fields)

    async def get_generation(self, id: str) -> dict[str, Any] | None:
        return await self._get(Generation, id)

    async def list_generations(
        self,
        project_id: str | None = None,
        *,
        kind: str | None = None,
        status: str | None = None,
        scene_id: str | None = None,
        execution_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        where = []
        for column, value in (
            (Generation.project_id, project_id), (Generation.kind, kind), (Generation.status, status),
            (Generation.scene_id, scene_id), (Generation.execution_id, execution_id),
        ):  # fmt: skip
            if value is not None:
                where.append(column == value)
        return await self._list(Generation, *where, order_by=Generation.created_at, limit=limit)

    async def find_cached_generation(self, cache_key: str) -> dict[str, Any] | None:
        rows = await self._list(
            Generation, Generation.cache_key == cache_key, Generation.status == "COMPLETED",
            Generation.output_path.is_not(None), order_by=Generation.created_at.desc(), limit=1,
        )  # fmt: skip
        return rows[0] if rows else None

    async def add_asset(self, **fields: Any) -> dict[str, Any]:
        fields.setdefault("id", new_id("ast"))
        fields.setdefault("created_at", utcnow())
        return await self._insert(Asset, **fields)

    async def list_assets(self, project_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        where = [Asset.project_id == project_id]
        if kind:
            where.append(Asset.kind == kind)
        return await self._list(Asset, *where, order_by=Asset.created_at)

    # -------------------------------------------------------------- costs
    async def add_cost(self, **fields: Any) -> dict[str, Any]:
        fields.setdefault("created_at", utcnow())
        return await self._insert(CostRecord, **fields)

    async def cost_sum(
        self, *, since: datetime | None = None, project_id: str | None = None, execution_id: str | None = None
    ) -> float:
        amount = func.coalesce(CostRecord.actual_usd, CostRecord.estimated_usd)
        stmt = select(func.coalesce(func.sum(amount), 0.0))
        if since is not None:
            stmt = stmt.where(CostRecord.created_at >= since)
        if project_id is not None:
            stmt = stmt.where(CostRecord.project_id == project_id)
        if execution_id is not None:
            stmt = stmt.where(CostRecord.execution_id == execution_id)
        async with self.db.session() as session:
            return float((await session.execute(stmt)).scalar_one() or 0.0)

    async def list_costs(self, limit: int = 500, project_id: str | None = None) -> list[dict[str, Any]]:
        where = [CostRecord.project_id == project_id] if project_id else []
        return await self._list(CostRecord, *where, order_by=CostRecord.id.desc(), limit=limit)

    # ---------------------------------------------------------- scheduler
    async def create_job(self, **fields: Any) -> dict[str, Any]:
        fields.setdefault("id", new_id("job"))
        fields.setdefault("created_at", utcnow())
        return await self._insert(ScheduledJob, **fields)

    async def update_job(self, id: str, **fields: Any) -> None:
        await self._update(ScheduledJob, id, **fields)

    async def get_job(self, id: str) -> dict[str, Any] | None:
        return await self._get(ScheduledJob, id)

    async def get_job_by_name(self, name: str) -> dict[str, Any] | None:
        rows = await self._list(ScheduledJob, ScheduledJob.name == name, limit=1)
        return rows[0] if rows else None

    async def list_jobs(self) -> list[dict[str, Any]]:
        return await self._list(ScheduledJob, order_by=ScheduledJob.created_at)

    async def delete_job(self, id: str) -> bool:
        async with self.db.write() as session:
            result = await session.execute(delete(ScheduledJob).where(ScheduledJob.id == id))
            return bool(result.rowcount)

    # -------------------------------------------------------------- cache
    async def cache_get(self, key: str) -> dict[str, Any] | None:
        async with self.db.write() as session:
            row = await session.get(CacheEntry, key)
            if row is None:
                return None
            row.hits += 1
            return dict(row.value)

    async def cache_put(self, key: str, kind: str, value: dict[str, Any]) -> None:
        async with self.db.write() as session:
            await session.merge(CacheEntry(key=key, kind=kind, value=value, hits=0, created_at=utcnow()))

    async def cache_delete(self, key: str) -> None:
        async with self.db.write() as session:
            await session.execute(delete(CacheEntry).where(CacheEntry.key == key))

    # -------------------------------------------------------------- stats
    async def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        async with self.db.session() as session:
            for name, model in (
                ("executions", Execution), ("tasks", TaskRow), ("approvals_pending", Approval),
                ("memories", Memory), ("projects", CreativeProject), ("generations", Generation),
                ("jobs", ScheduledJob),
            ):  # fmt: skip
                stmt = select(func.count()).select_from(model)
                if name == "approvals_pending":
                    stmt = stmt.where(Approval.status == ApprovalStatus.PENDING.value)
                out[name] = int((await session.execute(stmt)).scalar_one())
        return out


def _task_fields(task: Any) -> dict[str, Any]:
    return {
        "parent_id": task.parent_id,
        "description": task.description,
        "agent": task.agent,
        "action": task.action,
        "dependencies": list(task.dependencies),
        "status": str(task.status.value if hasattr(task.status, "value") else task.status),
        "priority": task.priority,
        "input": task.input,
        "output": task.output,
        "meta": task.metadata,
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
        "timeout": task.timeout,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "error": task.error,
    }


def _task_row(execution_id: str, task: Any, position: int) -> TaskRow:
    return TaskRow(id=task.id, execution_id=execution_id, position=position, **_task_fields(task))
