"""Миграция живой базы: рассылки «один чат» (до v2) → рассылки на несколько чатов."""

from __future__ import annotations

import sqlite3

from bot.db.base import Database
from bot.db.models import Campaign, Post
from bot.db.repo import Repo

OWNER = 1000


async def _add_chat(repo: Repo, tg_id: int, title: str):
    change = await repo.upsert_chat_membership(
        tg_id=tg_id,
        title=title,
        username=None,
        chat_type="supergroup",
        is_forum=True,
        in_chat=True,
        is_admin=True,
        can_post=True,
        can_pin=True,
        actor_id=OWNER,
        admin_ids=[OWNER],
    )
    return change.chat


async def make_v1_database(path) -> dict[str, int]:
    """База в том виде, в каком её оставила прошлая версия бота: рассылка хранит свой единственный
    чат (chat_id), тему и счётчики, таблицы campaign_chats и колонки kind ещё нет."""
    db = Database(path)
    await db.init()
    repo = Repo(db)
    chat_a = await _add_chat(repo, -100, "Чат A")
    chat_b = await _add_chat(repo, -200, "Чат B")
    async with db.session() as s:
        draft = Campaign(name="Шаблон", chat_id=None, times=["10:00"])
        s.add(draft)
        await s.flush()
        running = Campaign(
            name="Реклама",
            chat_id=chat_a.id,
            is_active=True,
            times=["09:00"],
            thread_id=45,
            last_message_ids=[7, 8],
            last_sent_ts=111,
            sent_count=3,
            fail_count=2,
            last_error="boom",
            source_draft_id=draft.id,
        )
        stopped = Campaign(name="Вторая", chat_id=chat_b.id, times=["12:00"])
        s.add_all([running, stopped])
        await s.flush()
        s.add_all(
            [
                Post(campaign_id=running.id, position=0, kind="text", payload={"text": "a"}),
                Post(campaign_id=draft.id, position=0, kind="text", payload={"text": "b"}),
            ]
        )
        await s.commit()
        ids = {"draft": draft.id, "running": running.id, "stopped": stopped.id, "a": chat_a.id, "b": chat_b.id}
    async with db.engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE campaign_chats")
        await conn.exec_driver_sql("DROP TABLE moderation_log")
        await conn.exec_driver_sql("ALTER TABLE campaigns DROP COLUMN kind")
        # Колонки защиты чатов появились позже — в старой базе их нет
        for column in (
            "can_delete",
            "can_invite",
            "can_restrict",
            "invite_link",
            "spam_filter",
            "sub_mode",
            "sub_channels",
            "auto_approve",
            "trusted",
        ):
            await conn.exec_driver_sql(f"ALTER TABLE chats DROP COLUMN {column}")
        await conn.exec_driver_sql("DELETE FROM settings WHERE key = 'schema_version'")
    await db.close()
    return ids


async def test_v1_database_is_migrated(tmp_path):
    path = tmp_path / "autopost.db"
    ids = await make_v1_database(path)

    db = Database(path)
    await db.init()
    repo = Repo(db)
    try:
        assert (await repo.load_settings())["schema_version"] == "2"
        running = await repo.get_campaign(ids["running"])
        assert running.kind == "campaign" and not running.is_draft
        assert running.chat_id is None and running.last_error is None
        assert running.is_active and running.source_draft_id == ids["draft"]
        assert (await repo.get_campaign(ids["draft"])).is_draft
        assert [c.id for c in await repo.list_drafts()] == [ids["draft"]]
        assert [c.id for c in await repo.list_campaigns()] == [ids["running"], ids["stopped"]]

        targets = await repo.campaign_targets(running.id)
        assert [t.chat.id for t in targets] == [ids["a"]]
        link = targets[0].link
        assert link.thread_id == 45 and link.last_message_ids == [7, 8] and link.last_sent_ts == 111
        assert link.sent_count == 3 and link.fail_count == 2 and link.last_error == "boom" and not link.paused
        assert [c.id for c in await repo.list_campaigns(ids["b"])] == [ids["stopped"]]
        assert len(await repo.list_posts(running.id)) == 1
        # Колонки новых функций дописаны сами: автоприём заявок — по общей настройке (включён)
        chat = await repo.get_chat(ids["a"])
        assert chat.auto_approve is None and chat.spam_filter is None and chat.sub_mode is None
        assert chat.trusted is None  # скрытых админов у чата нет
        assert chat.can_restrict is None and chat.can_invite is None  # права узнаются при первой проверке
        assert await repo.moderation_counts(0) == (0, 0)  # журнал защиты создан заново
    finally:
        await db.close()

    # Перед переносом сохранена копия базы в старом виде
    backup = tmp_path / "autopost-before-v2.db"
    with sqlite3.connect(backup) as conn:
        rows = conn.execute("SELECT id, chat_id FROM campaigns ORDER BY id").fetchall()
    assert rows == [(ids["draft"], None), (ids["running"], ids["a"]), (ids["stopped"], ids["b"])]

    # Повторный запуск ничего не дублирует и не трогает копию
    backup_mtime = backup.stat().st_mtime_ns
    db = Database(path)
    await db.init()
    repo = Repo(db)
    try:
        assert len(await repo.campaign_targets(ids["running"])) == 1
        assert backup.stat().st_mtime_ns == backup_mtime
        # Удаление чата теперь убирает его из рассылки, а не удаляет саму рассылку
        await repo.delete_chat(ids["a"])
        assert await repo.get_campaign(ids["running"]) is not None
        assert await repo.campaign_targets(ids["running"]) == []
        assert len(await repo.list_posts(ids["running"])) == 1
    finally:
        await db.close()


async def test_unfinished_backup_is_not_trusted(tmp_path):
    path = tmp_path / "autopost.db"
    await make_v1_database(path)
    (tmp_path / "autopost-before-v2.db.part").write_bytes(b"oops")  # запись копии оборвалась в прошлый раз
    db = Database(path)
    await db.init()
    await db.close()
    backup = tmp_path / "autopost-before-v2.db"
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT COUNT(*) FROM campaigns WHERE chat_id IS NOT NULL").fetchone() == (2,)
    assert not (tmp_path / "autopost-before-v2.db.part").exists()


async def test_fresh_database_starts_at_latest_version(tmp_path):
    db = Database(tmp_path / "fresh.db")
    await db.init()
    try:
        assert (await Repo(db).load_settings())["schema_version"] == "2"
    finally:
        await db.close()
    assert not (tmp_path / "fresh-before-v2.db").exists()
