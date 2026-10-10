from __future__ import annotations

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def create_engine(database_url: str) -> AsyncEngine:
    kwargs: dict = {"pool_pre_ping": True}
    if database_url.startswith("postgresql"):
        kwargs.update(pool_size=5, max_overflow=5, pool_recycle=1800)
    elif database_url.startswith("sqlite"):
        # Wait for a competing writer instead of failing with "database is locked".
        kwargs["connect_args"] = {"timeout": 30}
    engine = create_async_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")  # enforce FKs (off by default in SQLite)
            cursor.execute("PRAGMA busy_timeout=30000")
            if ":memory:" not in database_url and database_url.rstrip("/") != "sqlite+aiosqlite:":
                cursor.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def ping(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
