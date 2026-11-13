"""Engine/session management and programmatic Alembic migrations."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..errors import ConfigError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def run_migrations(url: str, revision: str = "head") -> None:
    """Blocking. Call from a worker thread when an event loop is already running."""
    command.upgrade(alembic_config(url), revision)


def head_revision(url: str) -> str | None:
    return ScriptDirectory.from_config(alembic_config(url)).get_current_head()


class Database:
    def __init__(self, url: str):
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        if self.is_sqlite:
            self.engine: AsyncEngine = create_async_engine(url, connect_args={"timeout": 30})
            event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)
        else:
            self.engine = create_async_engine(url, pool_size=10, max_overflow=10, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        # SQLite allows one writer at a time; serialising writers in-process avoids
        # "database is locked" under parallel task execution.
        self._write_lock = asyncio.Lock()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session:
            yield session

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncSession]:
        """Session that commits on exit and rolls back on error."""
        if self.is_sqlite:
            async with self._write_lock, self._sessions() as session, session.begin():
                yield session
        else:
            async with self._sessions() as session, session.begin():
                yield session

    def _restrict_sqlite_files(self) -> None:
        """The database holds memories and approvals: owner-only, including WAL side files."""
        if not self.is_sqlite or os.name != "posix":
            return
        path = Path(self.url.split("///", 1)[-1])
        for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            try:
                if candidate.is_file():
                    candidate.chmod(0o600)
            except OSError:
                pass

    async def migrate(self) -> None:
        await asyncio.to_thread(run_migrations, self.url)
        self._restrict_sqlite_files()

    async def current_revision(self) -> str | None:
        async with self.engine.connect() as conn:
            return await conn.run_sync(lambda sync: MigrationContext.configure(sync).get_current_revision())

    async def is_current(self) -> bool:
        return await self.current_revision() == head_revision(self.url)

    async def ensure_ready(self, auto_migrate: bool) -> None:
        if auto_migrate:
            await self.migrate()
            return
        self._restrict_sqlite_files()
        if not await self.is_current():
            raise ConfigError(
                "database schema is not at the latest migration",
                hint="run `super-agent db upgrade` (or set SAX_AUTO_MIGRATE=true)",
            )

    async def ping(self) -> bool:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    async def dispose(self) -> None:
        await self.engine.dispose()


def _sqlite_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()
