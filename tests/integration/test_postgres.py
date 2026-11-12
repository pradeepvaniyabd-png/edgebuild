"""Run the real migrations and core repo paths against PostgreSQL (set SAX_TEST_POSTGRES_URL)."""

from __future__ import annotations

import asyncio
import os

import pytest
from alembic import command
from sqlalchemy import text

from superagent.db.repo import Repo
from superagent.db.session import Database, alembic_config, run_migrations

URL = os.environ.get("SAX_TEST_POSTGRES_URL", "")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="SAX_TEST_POSTGRES_URL not set"),
]


@pytest.fixture
async def pg():
    database = Database(URL)
    async with database.write() as s:
        await s.execute(text("DROP SCHEMA public CASCADE"))
        await s.execute(text("CREATE SCHEMA public"))
    await asyncio.to_thread(run_migrations, URL, "head")
    yield database
    await database.dispose()


async def test_migrations_apply_and_are_current(pg: Database):
    assert await pg.is_current()
    async with pg.session() as s:
        tables = {r[0] for r in (await s.execute(text("select tablename from pg_tables where schemaname='public'"))).all()}
    assert {"executions", "tasks", "approvals", "memories", "generations", "cost_records"} <= tables


async def test_execution_task_cost_roundtrip(pg: Database):
    repo = Repo(pg)
    ex = await repo.create_execution(id="exe_pg1", command="pg smoke")
    assert (await repo.get_execution(ex["id"]))["command"] == "pg smoke"
    memory = await repo.add_memory(kind="fact", scope="pg", content="postgres works", embedding=[0.1, 0.2], tags=["t"])
    found = await repo.list_memories(scopes=["pg"])
    assert [m["id"] for m in found] == [memory["id"]]


async def test_downgrade_then_upgrade(pg: Database):
    await asyncio.to_thread(command.downgrade, alembic_config(URL), "base")
    async with pg.session() as s:
        left = (await s.execute(text("select count(*) from pg_tables where schemaname='public' and tablename<>'alembic_version'"))).scalar()
    assert left == 0
    await asyncio.to_thread(run_migrations, URL, "head")
    assert await pg.is_current()
