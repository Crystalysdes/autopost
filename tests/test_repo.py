from __future__ import annotations

from bot.db.repo import Repo, next_chat_status

OWNER = 1000


async def add_chat(repo: Repo, tg_id: int, *, actor: int = OWNER, title: str = "Чат", chat_type: str = "supergroup"):
    change = await repo.upsert_chat_membership(
        tg_id=tg_id,
        title=title,
        username=None,
        chat_type=chat_type,
        is_forum=False,
        in_chat=True,
        is_admin=True,
        can_post=True,
        can_pin=True,
        actor_id=actor,
        owner_id=OWNER,
    )
    return change


def test_next_chat_status_rules():
    assert next_chat_status(None, True, OWNER, OWNER) == "active"
    assert next_chat_status(None, True, 5, OWNER) == "pending"
    assert next_chat_status(None, True, None, OWNER) == "pending"
    assert next_chat_status("active", True, 5, OWNER) == "active"  # чужой админ поменял права
    assert next_chat_status("pending", True, OWNER, OWNER) == "active"
    assert next_chat_status("left", True, 5, OWNER) == "pending"
    assert next_chat_status("active", False, OWNER, OWNER) == "left"


async def test_upsert_and_leave_pauses_campaigns(repo: Repo):
    change = await add_chat(repo, -100)
    assert change.is_new and change.status == "active"
    chat = change.chat
    campaign = await repo.create_campaign(chat.id, "Реклама")
    await repo.update_campaign(campaign.id, is_active=True, next_run_ts=123, next_slot_ts=100)

    left = await repo.upsert_chat_membership(
        tg_id=-100,
        title="Чат",
        username=None,
        chat_type="supergroup",
        is_forum=False,
        in_chat=False,
        is_admin=False,
        can_post=False,
        can_pin=False,
        actor_id=5,
        owner_id=OWNER,
    )
    assert left.status == "left" and left.prev_status == "active" and left.paused == 1
    campaign = await repo.get_campaign(campaign.id)
    assert campaign.is_active is False and campaign.next_run_ts is None


async def test_lost_post_right_flag(repo: Repo):
    await add_chat(repo, -100, chat_type="channel")
    change = await repo.upsert_chat_membership(
        tg_id=-100,
        title="Канал",
        username=None,
        chat_type="channel",
        is_forum=False,
        in_chat=True,
        is_admin=True,
        can_post=False,
        can_pin=False,
        actor_id=5,
        owner_id=OWNER,
    )
    assert change.lost_post_right and change.status == "active"


async def test_unknown_chat_leave_is_noop(repo: Repo):
    change = await repo.upsert_chat_membership(
        tg_id=-5,
        title="x",
        username=None,
        chat_type="group",
        is_forum=False,
        in_chat=False,
        is_admin=False,
        can_post=False,
        can_pin=False,
        actor_id=OWNER,
        owner_id=OWNER,
    )
    assert change.chat is None
    assert await repo.list_chats() == []


async def test_posts_order_move_delete(repo: Repo):
    chat = (await add_chat(repo, -1)).chat
    campaign = await repo.create_campaign(chat.id, "R")
    ids = []
    for n in range(3):
        post, number = await repo.add_post(campaign.id, kind="text", payload={"text": str(n)})
        ids.append(post.id)
        assert number == n + 1
    assert await repo.move_post(ids[2], -1)
    assert [p.id for p in await repo.list_posts(campaign.id)] == [ids[0], ids[2], ids[1]]
    assert not await repo.move_post(ids[0], -1)
    assert await repo.delete_post(ids[2]) == campaign.id
    posts = await repo.list_posts(campaign.id)
    assert [p.id for p in posts] == [ids[0], ids[1]] and [p.position for p in posts] == [0, 1]


async def test_draft_apply_is_upsert_and_skips_thread(repo: Repo):
    chat_a = (await add_chat(repo, -1, title="A")).chat
    chat_b = (await add_chat(repo, -2, title="B")).chat
    source = await repo.create_campaign(chat_a.id, "Исходная")
    await repo.update_campaign(source.id, times=["09:00"], pin=True, thread_id=77, jitter_min=5)
    await repo.add_post(source.id, kind="text", payload={"text": "1"}, buttons=[[{"text": "a", "url": "https://a.ru"}]])

    draft = await repo.save_as_draft(source.id)
    assert draft.chat_id is None and draft.times == ["09:00"] and draft.pin is True and draft.thread_id is None
    assert (await repo.get_campaign(source.id)).source_draft_id == draft.id
    assert len(await repo.list_posts(draft.id)) == 1

    first = await repo.apply_template(draft.id, [chat_b.id], activate=True, link=True)
    assert [created for _, created in first] == [True]
    created = first[0][0]
    assert created.is_active and created.source_draft_id == draft.id and created.thread_id is None

    # меняем черновик и применяем снова — обновление, а не дубль
    await repo.update_campaign(draft.id, times=["10:00", "20:00"])
    await repo.add_post(draft.id, kind="text", payload={"text": "2"})
    second = await repo.apply_template(draft.id, [chat_b.id], activate=None, link=True)
    assert [created for _, created in second] == [False]
    assert second[0][0].id == created.id
    updated = await repo.get_campaign(created.id)
    assert updated.times == ["10:00", "20:00"] and updated.is_active is True
    assert len(await repo.list_posts(created.id)) == 2
    assert await repo.count_campaigns(chat_b.id) == 1

    linked = await repo.linked_campaigns(draft.id)
    assert {c.id for c in linked} == {source.id, created.id}


async def test_copy_without_link_creates_independent_campaigns(repo: Repo):
    chat_a = (await add_chat(repo, -1)).chat
    chat_b = (await add_chat(repo, -2)).chat
    source = await repo.create_campaign(chat_a.id, "R")
    await repo.add_post(source.id, kind="text", payload={"text": "1"})
    for _ in range(2):
        await repo.apply_template(source.id, [chat_b.id], activate=False, link=False)
    campaigns = await repo.list_campaigns(chat_b.id)
    assert len(campaigns) == 2 and all(c.source_draft_id is None for c in campaigns)


async def test_migration_moves_campaigns_and_is_idempotent(repo: Repo):
    old = (await add_chat(repo, -10, title="Группа", chat_type="group")).chat
    campaign = await repo.create_campaign(old.id, "R")
    # апдейт для новой супергруппы пришёл раньше сообщения о миграции
    new = (await add_chat(repo, -10010, actor=5, title="Группа")).chat
    assert new.status == "pending"

    merged = await repo.migrate_chat(-10, -10010)
    assert merged.id == new.id and merged.status == "active" and merged.type == "supergroup"
    assert (await repo.get_campaign(campaign.id)).chat_id == new.id
    assert await repo.get_chat(old.id) is None
    again = await repo.migrate_chat(-10, -10010)
    assert again.id == new.id


async def test_migration_simple_rename(repo: Repo):
    old = (await add_chat(repo, -10, chat_type="group")).chat
    moved = await repo.migrate_chat(-10, -10010)
    assert moved.id == old.id and moved.tg_id == -10010


async def test_delete_chat_cascades(repo: Repo):
    chat = (await add_chat(repo, -1)).chat
    campaign = await repo.create_campaign(chat.id, "R")
    await repo.add_post(campaign.id, kind="text", payload={"text": "1"})
    await repo.delete_chat(chat.id)
    assert await repo.get_campaign(campaign.id) is None
    assert await repo.list_posts(campaign.id) == []


async def test_settings_roundtrip(repo: Repo):
    await repo.save_setting("timezone", "Asia/Almaty")
    await repo.save_setting("timezone", "Europe/Moscow")
    assert await repo.load_settings() == {"timezone": "Europe/Moscow"}


async def test_backup_snapshot(db, repo: Repo, tmp_path):
    await add_chat(repo, -1)
    target = tmp_path / "backup.db"
    await db.backup_to(target)
    assert target.exists() and target.stat().st_size > 0
