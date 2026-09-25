from __future__ import annotations

from bot.db.repo import DRAFT, Repo, next_chat_status

OWNER = 1000


async def add_chat(
    repo: Repo,
    tg_id: int,
    *,
    actor: int = OWNER,
    title: str = "Чат",
    chat_type: str = "supergroup",
    can_post: bool = True,
    can_pin: bool = True,
):
    change = await repo.upsert_chat_membership(
        tg_id=tg_id,
        title=title,
        username=None,
        chat_type=chat_type,
        is_forum=False,
        in_chat=True,
        is_admin=True,
        can_post=can_post,
        can_pin=can_pin,
        actor_id=actor,
        admin_ids=[OWNER],
    )
    return change


async def leave(repo: Repo, tg_id: int):
    return await repo.upsert_chat_membership(
        tg_id=tg_id,
        title="Чат",
        username=None,
        chat_type="supergroup",
        is_forum=False,
        in_chat=False,
        is_admin=False,
        can_post=False,
        can_pin=False,
        actor_id=5,
        admin_ids=[OWNER],
    )


def test_next_chat_status_rules():
    admins = {OWNER, 77}
    assert next_chat_status(None, True, OWNER, admins) == "active"
    assert next_chat_status(None, True, 77, admins) == "active"  # второй админ
    assert next_chat_status(None, True, 5, admins) == "pending"
    assert next_chat_status(None, True, None, admins) == "pending"
    assert next_chat_status("active", True, 5, admins) == "active"  # чужой админ чата поменял права
    assert next_chat_status("pending", True, OWNER, admins) == "active"
    assert next_chat_status("left", True, 5, admins) == "pending"
    assert next_chat_status("active", False, OWNER, admins) == "left"


async def test_leaving_chat_keeps_campaign_and_skips_chat(repo: Repo):
    chat = (await add_chat(repo, -100)).chat
    other = (await add_chat(repo, -200)).chat
    campaign = await repo.create_campaign("Реклама", chat_ids=[chat.id, other.id])
    await repo.update_campaign(campaign.id, is_active=True, next_run_ts=123, next_slot_ts=100)

    left = await leave(repo, -100)
    assert left.status == "left" and left.prev_status == "active"
    campaign = await repo.get_campaign(campaign.id)
    assert campaign.is_active is True and campaign.next_run_ts == 123
    targets = await repo.campaign_targets(campaign.id)
    assert {t.chat.id: t.deliverable for t in targets} == {chat.id: False, other.id: True}

    # Бота вернули — чат снова в работе без ручного запуска
    await add_chat(repo, -100)
    assert all(t.deliverable for t in await repo.campaign_targets(campaign.id))


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
        admin_ids=[OWNER],
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
        admin_ids=[OWNER],
    )
    assert change.chat is None
    assert await repo.list_chats() == []


async def test_posts_order_move_delete(repo: Repo):
    campaign = await repo.create_campaign("R")
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


async def test_targets_toggle_all_and_none(repo: Repo):
    chat_a = (await add_chat(repo, -1, title="Б-чат")).chat
    chat_b = (await add_chat(repo, -2, title="а-чат")).chat
    chat_c = (await add_chat(repo, -3, title="В-чат")).chat
    campaign = await repo.create_campaign("R", chat_ids=[chat_a.id])
    other = await repo.create_campaign("R2", chat_ids=[chat_a.id, chat_b.id])

    assert await repo.set_target(campaign.id, chat_b.id, True)
    assert not await repo.set_target(campaign.id, chat_b.id, True)  # уже отмечен — без дублей
    assert [t.chat.id for t in await repo.campaign_targets(campaign.id)] == [chat_b.id, chat_a.id]  # по алфавиту
    assert await repo.set_target(campaign.id, chat_a.id, False)
    assert not await repo.set_target(campaign.id, chat_a.id, False)
    assert not await repo.set_target(campaign.id, 999, True)  # чата нет

    assert await repo.add_targets(campaign.id, [chat_a.id, chat_b.id, chat_c.id]) == 2
    assert len(await repo.campaign_targets(campaign.id)) == 3
    assert [c.id for c in await repo.list_campaigns(chat_a.id)] == [campaign.id, other.id]
    assert await repo.count_campaigns(chat_a.id) == 2 and await repo.count_campaigns() == 2
    await repo.update_campaign(other.id, is_active=True)
    stats = await repo.campaign_stats_by_chat()
    assert stats[chat_a.id] == (2, 1) and stats[chat_c.id] == (1, 0)

    assert await repo.clear_targets(campaign.id) == 3
    assert await repo.campaign_targets(campaign.id) == []
    assert len(await repo.campaign_targets(other.id)) == 2  # другие рассылки не задеты


async def test_target_failures_pause_only_that_chat(repo: Repo):
    chat_a = (await add_chat(repo, -1)).chat
    chat_b = (await add_chat(repo, -2)).chat
    campaign = await repo.create_campaign("R", chat_ids=[chat_a.id, chat_b.id])
    for attempt in range(1, 4):
        fails, paused = await repo.record_target_failed(
            campaign.id, chat_a.id, None, "boom", manual=False, now=1, max_fails=3
        )
        assert fails == attempt and paused == (attempt == 3)
    targets = {t.chat.id: t for t in await repo.campaign_targets(campaign.id)}
    assert targets[chat_a.id].link.paused and not targets[chat_a.id].deliverable
    assert targets[chat_b.id].deliverable
    assert (await repo.get_campaign(campaign.id)).is_active is False  # статус рассылки не трогаем

    await repo.record_target_sent(campaign.id, chat_b.id, None, [5, 6], manual=False, now=2)
    link_b = (await repo.update_target(campaign.id, chat_b.id)).last_message_ids
    assert link_b == [5, 6]

    assert await repo.resume_target(campaign.id, chat_a.id)
    resumed = {t.chat.id: t for t in await repo.campaign_targets(campaign.id)}[chat_a.id]
    assert resumed.deliverable and resumed.link.fail_count == 0 and resumed.link.last_error is None


async def test_draft_apply_makes_one_campaign_with_union_of_chats(repo: Repo):
    chat_a = (await add_chat(repo, -1, title="A")).chat
    chat_b = (await add_chat(repo, -2, title="B")).chat
    chat_c = (await add_chat(repo, -3, title="C")).chat
    draft = await repo.create_campaign("Шаблон", kind=DRAFT)
    await repo.update_campaign(draft.id, times=["09:00"], pin=True, jitter_min=5)
    await repo.add_post(draft.id, kind="text", payload={"text": "1"}, buttons=[[{"text": "a", "url": "https://a.ru"}]])
    assert draft.is_draft and [d.id for d in await repo.list_drafts()] == [draft.id]

    campaign, created = await repo.apply_draft(draft.id, [chat_a.id, chat_b.id], activate=False)
    assert created and not campaign.is_draft and not campaign.is_active and campaign.source_draft_id == draft.id
    assert campaign.times == ["09:00"] and campaign.pin is True
    assert [p.buttons for p in await repo.list_posts(campaign.id)] == [[[{"text": "a", "url": "https://a.ru"}]]]

    # Правим черновик и применяем к другому чату — та же рассылка, чаты добавляются
    await repo.update_campaign(draft.id, times=["10:00", "20:00"])
    await repo.add_post(draft.id, kind="text", payload={"text": "2"})
    again, created = await repo.apply_draft(draft.id, [chat_b.id, chat_c.id], activate=True)
    assert not created and again.id == campaign.id and again.is_active
    assert {t.chat.id for t in await repo.campaign_targets(campaign.id)} == {chat_a.id, chat_b.id, chat_c.id}
    updated = await repo.get_campaign(campaign.id)
    assert updated.times == ["10:00", "20:00"] and len(await repo.list_posts(campaign.id)) == 2
    assert await repo.count_campaigns() == 1
    assert await repo.linked_counts() == {draft.id: 3}
    assert await repo.apply_draft(campaign.id, [chat_a.id], activate=False) is None  # это не черновик


async def test_save_as_draft_links_source_and_sync_keeps_chats(repo: Repo):
    chat_a = (await add_chat(repo, -1)).chat
    chat_b = (await add_chat(repo, -2)).chat
    source = await repo.create_campaign("Исходная", chat_ids=[chat_a.id])
    await repo.update_campaign(source.id, times=["09:00"], is_active=True)
    await repo.add_post(source.id, kind="text", payload={"text": "1"})

    draft = await repo.save_as_draft(source.id)
    assert draft.is_draft and draft.times == ["09:00"] and not draft.is_active
    assert (await repo.get_campaign(source.id)).source_draft_id == draft.id
    assert await repo.campaign_targets(draft.id) == []

    # Применение черновика добавляет чаты в ту же (исходную) рассылку, а не создаёт копию
    campaign, created = await repo.apply_draft(draft.id, [chat_b.id], activate=False)
    assert not created and campaign.id == source.id and campaign.is_active  # статус не сброшен
    assert {t.chat.id for t in await repo.campaign_targets(source.id)} == {chat_a.id, chat_b.id}

    await repo.update_campaign(draft.id, times=["08:00"])
    await repo.add_post(draft.id, kind="text", payload={"text": "2"})
    synced = await repo.sync_draft(draft.id)
    assert [c.id for c in synced] == [source.id]
    fresh = await repo.get_campaign(source.id)
    assert fresh.times == ["08:00"] and fresh.is_active and len(await repo.list_posts(source.id)) == 2
    assert len(await repo.campaign_targets(source.id)) == 2


async def test_migration_moves_targets_and_merges_duplicates(repo: Repo):
    old = (await add_chat(repo, -10, title="Группа", chat_type="group")).chat
    both = await repo.create_campaign("Обе", chat_ids=[old.id])
    only_old = await repo.create_campaign("Старая", chat_ids=[old.id])
    await repo.record_target_sent(only_old.id, old.id, None, [11], manual=False, now=1)
    # апдейт для новой супергруппы пришёл раньше сообщения о миграции
    new = (await add_chat(repo, -10010, actor=5, title="Группа")).chat
    assert new.status == "pending"
    await repo.set_target(both.id, new.id, True)

    merged = await repo.migrate_chat(-10, -10010)
    assert merged.id == new.id and merged.status == "active" and merged.type == "supergroup"
    assert [t.chat.id for t in await repo.campaign_targets(both.id)] == [new.id]  # без дубля
    moved = await repo.campaign_targets(only_old.id)
    assert [t.chat.id for t in moved] == [new.id]
    assert moved[0].link.last_message_ids == [] and moved[0].link.sent_count == 1
    assert await repo.get_chat(old.id) is None
    again = await repo.migrate_chat(-10, -10010)
    assert again.id == new.id


async def test_migration_simple_rename(repo: Repo):
    old = (await add_chat(repo, -10, chat_type="group")).chat
    campaign = await repo.create_campaign("R", chat_ids=[old.id])
    await repo.record_target_sent(campaign.id, old.id, None, [3], manual=False, now=1)
    moved = await repo.migrate_chat(-10, -10010)
    assert moved.id == old.id and moved.tg_id == -10010
    assert (await repo.campaign_targets(campaign.id))[0].link.last_message_ids == []


async def test_delete_chat_keeps_campaign(repo: Repo):
    chat = (await add_chat(repo, -1)).chat
    other = (await add_chat(repo, -2)).chat
    campaign = await repo.create_campaign("R", chat_ids=[chat.id, other.id])
    await repo.add_post(campaign.id, kind="text", payload={"text": "1"})
    await repo.delete_chat(chat.id)
    assert await repo.get_campaign(campaign.id) is not None
    assert [t.chat.id for t in await repo.campaign_targets(campaign.id)] == [other.id]
    assert len(await repo.list_posts(campaign.id)) == 1


async def test_library_queries(repo: Repo):
    first = await repo.create_campaign("Первая")
    draft = await repo.create_campaign("Шаблон", kind=DRAFT)
    a, _ = await repo.add_post(first.id, kind="text", payload={"text": "a"})
    b, _ = await repo.add_post(draft.id, kind="text", payload={"text": "b"}, buttons=[[{"text": "x", "url": "y"}]])
    c, _ = await repo.add_post(first.id, kind="text", payload={"text": "c"})

    assert await repo.count_posts_total() == 3
    page = await repo.list_all_posts(0, 2)
    assert [(post.id, campaign.name) for post, campaign in page] == [(c.id, "Первая"), (b.id, "Шаблон")]
    assert [post.id for post, _ in await repo.list_all_posts(2, 2)] == [a.id]

    copied, number = await repo.copy_post(b.id, first.id)
    assert number == 3 and copied.campaign_id == first.id and copied.buttons == [[{"text": "x", "url": "y"}]]
    assert copied.id != b.id and (await repo.get_post(b.id)).campaign_id == draft.id
    assert await repo.copy_post(b.id, 999) is None

    fresh = await repo.campaign_from_post(a.id, "Рассылка 2")
    assert not fresh.is_draft and not fresh.is_active and await repo.campaign_targets(fresh.id) == []
    assert [p.payload for p in await repo.list_posts(fresh.id)] == [{"text": "a"}]
    assert await repo.campaign_from_post(999, "x") is None


async def test_settings_roundtrip(repo: Repo):
    await repo.save_setting("timezone", "Asia/Almaty")
    await repo.save_setting("timezone", "Europe/Moscow")
    settings = await repo.load_settings()
    assert settings["timezone"] == "Europe/Moscow"
    assert set(settings) == {"timezone", "schema_version"}


async def test_backup_snapshot(db, repo: Repo, tmp_path):
    await add_chat(repo, -1)
    target = tmp_path / "backup.db"
    await db.backup_to(target)
    assert target.exists() and target.stat().st_size > 0
