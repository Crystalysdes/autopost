"""Подключение к SQLite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from bot.db.models import Base


def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


class Database:
    """Одно соединение на весь процесс: запросы к SQLite идут строго по очереди,
    поэтому «database is locked» не возникает. Транзакции держим короткими и никогда
    не ждём Telegram внутри них."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{self.path}",
            pool_size=1,
            max_overflow=0,
            pool_timeout=120,
        )
        event.listen(self.engine.sync_engine, "connect", _set_sqlite_pragmas)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    def session(self) -> AsyncSession:
        return self.sessionmaker()

    async def backup_to(self, target: Path) -> None:
        """Согласованный снимок базы (работает и при включённом WAL)."""
        target.unlink(missing_ok=True)  # noqa: ASYNC240 - мгновенная операция
        async with self.engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.exec_driver_sql("VACUUM INTO ?", (str(target),))

    async def close(self) -> None:
        await self.engine.dispose()
