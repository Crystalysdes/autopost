"""Перенос данных между версиями бота.

Таблицы и новые колонки создаёт ``base.py`` (create_all + дописывание колонок), здесь — только данные.
Версия хранится в settings.schema_version. Каждый шаг идемпотентен: повторный запуск ничего не портит.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

LATEST_VERSION = 2

# v2: рассылка больше не принадлежит одному чату — чаты и состояние отправки в campaign_chats
_MIGRATE_V2 = (
    "UPDATE campaigns SET kind = CASE WHEN chat_id IS NULL THEN 'draft' ELSE 'campaign' END WHERE kind IS NULL",
    """
    INSERT INTO campaign_chats
        (campaign_id, chat_id, thread_id, last_message_ids, last_sent_ts, sent_count, fail_count, last_error,
         paused, created_ts)
    SELECT c.id, c.chat_id, c.thread_id, COALESCE(c.last_message_ids, '[]'), c.last_sent_ts,
           COALESCE(c.sent_count, 0), COALESCE(c.fail_count, 0), c.last_error, 0,
           COALESCE(c.created_ts, CAST(strftime('%s', 'now') AS INTEGER))
    FROM campaigns AS c
    WHERE c.chat_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM campaign_chats AS cc WHERE cc.campaign_id = c.id AND cc.chat_id = c.chat_id
      )
    """,
    # Отвязываем рассылки от чатов: иначе удаление чата (ON DELETE CASCADE) удалило бы всю рассылку
    "UPDATE campaigns SET chat_id = NULL, last_error = NULL WHERE chat_id IS NOT NULL",
)


async def _version(db: Any) -> int | None:
    async with db.engine.connect() as conn:
        row = (await conn.execute(text("SELECT value FROM settings WHERE key = 'schema_version'"))).first()
    return int(row[0]) if row else None


async def _has_campaigns(db: Any) -> bool:
    async with db.engine.connect() as conn:
        return (await conn.execute(text("SELECT 1 FROM campaigns LIMIT 1"))).first() is not None


async def _set_version(conn: Any, version: int) -> None:
    await conn.execute(
        text("INSERT OR REPLACE INTO settings (key, value) VALUES ('schema_version', :v)"), {"v": str(version)}
    )


async def run_migrations(db: Any) -> None:
    """db — bot.db.base.Database. Вызывается после создания таблиц."""
    version = await _version(db)
    if version is None and not await _has_campaigns(db):
        async with db.engine.begin() as conn:  # новая база: переносить нечего
            await _set_version(conn, LATEST_VERSION)
        return
    version = version or 1

    if version < 2:
        backup = db.path.with_name(f"{db.path.stem}-before-v2.db")
        if not backup.exists():
            # Готовая копия появляется под своим именем только целиком: если запись оборвётся,
            # при следующем запуске копия будет сделана заново, а не принята недописанной
            partial = backup.with_name(backup.name + ".part")
            await db.backup_to(partial)
            partial.replace(backup)
            logger.info("Перед обновлением базы сохранена копия: %s", backup)
        async with db.engine.begin() as conn:
            for statement in _MIGRATE_V2:
                await conn.execute(text(statement))
            await _set_version(conn, 2)
        logger.info("База обновлена до версии 2: рассылки теперь публикуются в несколько чатов")
