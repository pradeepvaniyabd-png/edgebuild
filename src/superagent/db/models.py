"""ORM models. Schema changes go through Alembic migrations, never ``create_all``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, Index, Integer, MetaData, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..utils import utcnow

JsonType = JSON().with_variant(JSONB(), "postgresql")
BigIntPk = BigInteger().with_variant(Integer(), "sqlite")

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _ts(**kwargs: Any) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kwargs)


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    command: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="cli")
    status: Mapped[str] = mapped_column(String(24), default="PLANNING")
    intent: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    result: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = _ts(default=utcnow)
    started_at: Mapped[datetime | None] = _ts(nullable=True)
    completed_at: Mapped[datetime | None] = _ts(nullable=True)

    __table_args__ = (
        Index("ix_executions_status", "status"),
        Index("ix_executions_created_at", "created_at"),
        Index("ix_executions_project_id", "project_id"),
    )


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    execution_id: Mapped[str] = mapped_column(String(40))
    parent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text)
    agent: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    dependencies: Mapped[list] = mapped_column(JsonType, default=list)
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    priority: Mapped[int] = mapped_column(Integer, default=5)
    input: Mapped[dict] = mapped_column(JsonType, default=dict)
    output: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    meta: Mapped[dict] = mapped_column("metadata", JsonType, default=dict)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    timeout: Mapped[float] = mapped_column(Float, default=900.0)
    created_at: Mapped[datetime] = _ts(default=utcnow)
    started_at: Mapped[datetime | None] = _ts(nullable=True)
    completed_at: Mapped[datetime | None] = _ts(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_tasks_execution_id_status", "execution_id", "status"),
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_created_at", "created_at"),
    )


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    execution_id: Mapped[str] = mapped_column(String(40))
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = _ts(default=utcnow)

    __table_args__ = (Index("ix_task_events_execution_id_id", "execution_id", "id"),)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    execution_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    action: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(40))
    risk: Mapped[int] = mapped_column(Integer, default=2)
    reason: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JsonType, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    scope: Mapped[str] = mapped_column(String(16), default="once")
    requested_at: Mapped[datetime] = _ts(default=utcnow)
    decided_at: Mapped[datetime | None] = _ts(nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_approvals_status", "status"),
        Index("ix_approvals_execution_id", "execution_id"),
        Index("ix_approvals_fingerprint", "fingerprint"),
    )


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    scope: Mapped[str] = mapped_column(String(64), default="global")
    content: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JsonType, default=list)
    embedding: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts(default=utcnow)
    updated_at: Mapped[datetime] = _ts(default=utcnow, onupdate=utcnow)
    last_accessed_at: Mapped[datetime | None] = _ts(nullable=True)

    __table_args__ = (
        Index("ix_memories_kind", "kind"),
        Index("ix_memories_scope", "scope"),
        Index("ix_memories_created_at", "created_at"),
    )


class CreativeProject(Base):
    __tablename__ = "creative_projects"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="CREATED")
    brief: Mapped[dict] = mapped_column(JsonType, default=dict)
    root_dir: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(default=utcnow)
    updated_at: Mapped[datetime] = _ts(default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_creative_projects_status", "status"),
        Index("ix_creative_projects_created_at", "created_at"),
    )


class Generation(Base):
    __tablename__ = "generations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="QUEUED")
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    params: Mapped[dict] = mapped_column(JsonType, default=dict)
    cache_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    qc: Mapped[dict | None] = mapped_column(JsonType, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts(default=utcnow)
    completed_at: Mapped[datetime | None] = _ts(nullable=True)

    __table_args__ = (
        Index("ix_generations_project_id_status", "project_id", "status"),
        Index("ix_generations_status", "status"),
        Index("ix_generations_cache_key", "cache_key"),
        Index("ix_generations_created_at", "created_at"),
        Index("ix_generations_execution_id", "execution_id"),
    )


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(40))
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column("metadata", JsonType, default=dict)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _ts(default=utcnow)

    __table_args__ = (Index("ix_assets_project_id_kind", "project_id", "kind"),)


class CostRecord(Base):
    __tablename__ = "cost_records"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    execution_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(24))
    units: Mapped[dict] = mapped_column(JsonType, default=dict)
    estimated_usd: Mapped[float] = mapped_column(Float, default=0.0)
    actual_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    priced: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _ts(default=utcnow)

    __table_args__ = (
        Index("ix_cost_records_created_at", "created_at"),
        Index("ix_cost_records_project_id", "project_id"),
        Index("ix_cost_records_execution_id", "execution_id"),
    )


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    command: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(String(16))
    expression: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_run_at: Mapped[datetime | None] = _ts(nullable=True)
    next_run_at: Mapped[datetime | None] = _ts(nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    last_execution_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts(default=utcnow)

    __table_args__ = (Index("ix_scheduled_jobs_status", "status"),)


class CacheEntry(Base):
    __tablename__ = "cache_entries"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))
    value: Mapped[dict] = mapped_column(JsonType, default=dict)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = _ts(default=utcnow)

    __table_args__ = (Index("ix_cache_entries_kind", "kind"),)
