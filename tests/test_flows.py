"""Сценарии целиком: апдейты идут через настоящий диспетчер aiogram, запросы бота записываются."""

from __future__ import annotations

import asyncio
import itertools
import random
from datetime import datetime
from typing import Any

import pytest
from aiogram import Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberUpdated,
    Message,
    MessageEntity,
    Update,
    User,
)

from bot.services.scheduler import Scheduler
from bot.setup import build_dispatcher
from bot.states import Input
from bot.ui.callbacks import ApplyDraft, CampAct, ChatAct, Nav, OptAct, PickAct, PostAct, SchedAct, SetAct
from tests.conftest import BOT_ID, OWNER_ID, STRANGER_ID, FakeClock, admin_member, left_member

OWNER = User(id=OWNER_ID, is_bot=False, first_name="Owner")
STRANGER = User(id=STRANGER_ID, is_bot=False, first_name="Stranger")
BOT_USER = User(id=BOT_ID, is_bot=True, first_name="Autopost")
_ids = itertools.count(1)


def private_message(text: str | None = None, user: User = OWNER, **fields: Any) -> Update:
    message = Message(
        message_id=next(_ids),
        date=datetime.now(),
        chat=Chat(id=user.id, type="private"),
        from_user=user,
        text=text,
        **fields,
    )
    return Update(update_id=next(_ids), message=message)


def group_message(chat_id: int, user: User = STRANGER, **fields: Any) -> Update:
    message = Message(
        message_id=next(_ids),
        date=datetime.now(),
        chat=Chat(id=chat_id, type="supergroup", title="Группа"),
        from_user=user,
        **fields,
    )
    return Update(update_id=next(_ids), message=message)


def press(data: Any, user: User = OWNER) -> Update:
    panel = Message(
        message_id=500,
        date=datetime.now(),
        chat=Chat(id=user.id, type="private"),
        from_user=BOT_USER,
        text="панель",
    )
    query = CallbackQuery(
        id=str(next(_ids)),
        from_user=user,
        chat_instance="ci",
        message=panel,
        data=data.pack() if hasattr(data, "pack") else data,
    )
    return Update(update_id=next(_ids), callback_query=query)


def membership(chat_id: int, *, actor: User = OWNER, new: Any = None, chat_type: str = "supergroup") -> Update:
    event = ChatMemberUpdated(
        chat=Chat(id=chat_id, type=chat_type, title=f"Чат {chat_id}"),
        from_user=actor,
        date=datetime.now(),
        old_chat_member=left_member(),
        new_chat_member=new if new is not None else admin_member(),
    )
    return Update(update_id=next(_ids), my_chat_member=event)


def owner_texts(session) -> list[str]:
    """Тексты сообщений владельцу (новые и отредактированные) в хронологическом порядке."""
    return [r.text for r in session.requests if isinstance(r, (SendMessage, EditMessageText)) and r.chat_id == OWNER_ID]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 9, 25, 8, 0).timestamp())


_DISPATCHER: Dispatcher | None = None


@pytest.fixture
def dp(app, clock) -> Dispatcher:
    # Роутеры — синглтоны модулей, поэтому диспетчер собираем один раз на весь прогон,
    # а для каждого теста подменяем контейнер зависимостей и хранилище состояний.
    global _DISPATCHER
    app.scheduler = Scheduler(app, clock=clock, rng=random.Random(0))
    if _DISPATCHER is None:
        _DISPATCHER = build_dispatcher(app, album_latency=0.05)
    _DISPATCHER["app"] = app
    _DISPATCHER.fsm.storage = MemoryStorage()
    return _DISPATCHER


@pytest.fixture
def feed(dp, bot):
    async def _feed(*updates: Update) -> None:
        for update in updates:
            await dp.feed_update(bot, update)

    return _feed


async def state_of(dp, bot, user_id: int = OWNER_ID) -> str | None:
    key = StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id)
    return await dp.fsm.storage.get_state(key)


async def add_chat(feed, app, chat_id: int, **kwargs: Any):
    await feed(membership(chat_id, **kwargs))
    return await app.repo.get_chat_by_tg(chat_id)


# ------------------------------------------------------------------------- доступ


async def test_start_from_owner_shows_menu_and_add_buttons(feed, session):
    await feed(private_message("/start"))
    greeting = next(c for c in session.calls(SendMessage) if c.reply_markup and hasattr(c.reply_markup, "keyboard"))
    buttons = greeting.reply_markup.keyboard[0]
    assert buttons[0].request_chat.chat_is_channel is False
    assert buttons[1].request_chat.chat_is_channel is True
    assert buttons[1].request_chat.bot_administrator_rights.can_post_messages is True
    assert any("Автопостинг" in text for text in owner_texts(session))


async def test_strangers_are_ignored(feed, session):
    await feed(private_message("/start", user=STRANGER), press(Nav(to="chats"), user=STRANGER))
    assert session.requests == []


async def test_group_chatter_is_ignored(feed, session):
    await feed(group_message(-100123, text="всем привет"))
    assert session.requests == []


# -------------------------------------------------------------------------- чаты


async def test_owner_adds_bot(feed, app, session):
    chat = await add_chat(feed, app, -100500)
    assert chat.status == "active" and chat.can_post and chat.can_pin
    assert "Бот добавлен" in owner_texts(session)[-1]


async def test_stranger_adds_bot_owner_accepts(feed, app, session):
    chat = await add_chat(feed, app, -100600, actor=STRANGER)
    assert chat.status == "pending"
    assert "другой человек" in owner_texts(session)[-1]
    await feed(press(ChatAct(a="accept", id=chat.id)))
    assert (await app.repo.get_chat(chat.id)).status == "active"


async def test_bot_removed_pauses_and_notifies(feed, app, session):
    chat = await add_chat(feed, app, -100650)
    campaign = await app.repo.create_campaign(chat.id, "R")
    await app.repo.update_campaign(campaign.id, is_active=True, next_run_ts=1, next_slot_ts=1)
    await feed(membership(-100650, actor=STRANGER, new=left_member()))
    assert (await app.repo.get_chat(chat.id)).status == "left"
    assert (await app.repo.get_campaign(campaign.id)).is_active is False
    assert "потерял доступ" in owner_texts(session)[-1]


async def test_chat_shared_registers_chat(feed, app, session):
    from aiogram.types import ChatShared

    await feed(private_message(chat_shared=ChatShared(request_id=1, chat_id=-100777)))
    chat = await app.repo.get_chat_by_tg(-100777)
    assert chat is not None and chat.status == "active"
    assert any("Права бота" in text for text in owner_texts(session))


async def test_group_migration_message(feed, app):
    chat = await add_chat(feed, app, -55, chat_type="group")
    await feed(group_message(-55, migrate_to_chat_id=-100555))
    assert (await app.repo.get_chat(chat.id)).tg_id == -100555


# ---------------------------------------------------------------------- рассылка


async def test_full_campaign_flow(feed, dp, bot, app, session, clock):
    chat = await add_chat(feed, app, -100700)
    await feed(press(ChatAct(a="new", id=chat.id)))
    campaign = (await app.repo.list_campaigns(chat.id))[0]

    await feed(press(CampAct(a="add", id=campaign.id)))
    assert await state_of(dp, bot) == Input.posts.state
    premium = MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="5368")
    await feed(private_message("🔥 Акция недели", entities=[premium]))
    await feed(press(CampAct(a="done", id=campaign.id)))
    assert await state_of(dp, bot) is None
    posts = await app.repo.list_posts(campaign.id)
    assert len(posts) == 1 and posts[0].payload["entities"][0]["custom_emoji_id"] == "5368"

    await feed(press(CampAct(a="on", id=campaign.id)))  # без расписания — отказ
    answers = session.calls(AnswerCallbackQuery)
    assert answers[-1].show_alert and "время" in answers[-1].text

    await feed(press(SchedAct(a="times", id=campaign.id)), private_message("9:00, 18:00"))
    campaign = await app.repo.get_campaign(campaign.id)
    assert campaign.times == ["09:00", "18:00"]

    await feed(press(OptAct(a="silent", id=campaign.id)), press(CampAct(a="on", id=campaign.id)))
    campaign = await app.repo.get_campaign(campaign.id)
    assert campaign.is_active and campaign.silent and campaign.next_run_ts

    clock.value = campaign.next_run_ts + 1
    await app.scheduler.tick()
    await app.scheduler.drain()
    sent = [c for c in session.calls(SendMessage) if c.chat_id == -100700]
    assert len(sent) == 1
    assert sent[0].entities[0].custom_emoji_id == "5368" and sent[0].parse_mode is None
    assert sent[0].disable_notification is True


async def test_album_is_one_post(feed, dp, bot, app, session):
    chat = await add_chat(feed, app, -100800)
    campaign = await app.repo.create_campaign(chat.id, "R")
    await feed(press(CampAct(a="add", id=campaign.id)))
    photos = [
        private_message(
            media_group_id="album1",
            photo=[{"file_id": f"p{i}", "file_unique_id": f"u{i}", "width": 10, "height": 10}],
            caption="Подпись" if i == 0 else None,
        )
        for i in range(3)
    ]
    await asyncio.gather(*(dp.feed_update(bot, update) for update in photos))
    posts = await app.repo.list_posts(campaign.id)
    assert len(posts) == 1 and posts[0].kind == "album"
    assert [item["file_id"] for item in posts[0].payload["items"]] == ["p0", "p1", "p2"]
    replies = [t for t in owner_texts(session) if t.startswith("✅ Пост #")]
    assert len(replies) == 1


async def test_unsupported_content_is_rejected(feed, app, session):
    chat = await add_chat(feed, app, -100810)
    campaign = await app.repo.create_campaign(chat.id, "R")
    await feed(press(CampAct(a="add", id=campaign.id)))
    await feed(private_message(dice={"emoji": "🎲", "value": 3}))
    assert await app.repo.list_posts(campaign.id) == []
    assert "не поддерживается" in owner_texts(session)[-1]


async def test_buttons_flow_and_validation(feed, dp, bot, app, session):
    chat = await add_chat(feed, app, -100900)
    campaign = await app.repo.create_campaign(chat.id, "R")
    post, _ = await app.repo.add_post(campaign.id, kind="text", payload={"text": "Пост"})

    await feed(press(PostAct(a="btn", id=post.id)), private_message("Кнопка без ссылки"))
    assert "Строка 1" in owner_texts(session)[-1]
    assert await state_of(dp, bot) == Input.buttons.state

    await feed(private_message("Сайт - example.com | Код - copy:SALE - green"))
    saved = await app.repo.get_post(post.id)
    assert saved.buttons == [
        [{"text": "Сайт", "url": "https://example.com"}, {"text": "Код", "copy_text": "SALE", "style": "success"}]
    ]
    preview = [c for c in session.calls(SendMessage) if c.chat_id == OWNER_ID and c.text == "Пост"]
    assert preview and preview[-1].reply_markup.inline_keyboard[0][0].url == "https://example.com"
    assert await state_of(dp, bot) is None


async def test_draft_save_and_apply_via_picker(feed, app):
    chat_a = await add_chat(feed, app, -101001)
    chat_b = await add_chat(feed, app, -101002)
    campaign = await app.repo.create_campaign(chat_a.id, "Реклама")
    await app.repo.add_post(campaign.id, kind="text", payload={"text": "Пост"})
    await app.repo.update_campaign(campaign.id, times=["12:00"])

    await feed(press(CampAct(a="todraft", id=campaign.id)))
    draft = (await app.repo.list_drafts())[0]
    await feed(
        press(CampAct(a="apply", id=draft.id)),
        press(PickAct(a="t", v=chat_b.id)),
        press(PickAct(a="go", v=1)),
    )
    created = await app.repo.list_campaigns(chat_b.id)
    assert len(created) == 1 and created[0].is_active and created[0].next_run_ts
    assert created[0].source_draft_id == draft.id
    linked = {c.id for c in await app.repo.linked_campaigns(draft.id)}
    assert linked == {campaign.id, created[0].id}

    # Правим черновик и обновляем во всех чатах
    await app.repo.update_campaign(draft.id, times=["08:00", "20:00"])
    await feed(press(CampAct(a="sync_ok", id=draft.id)))
    assert (await app.repo.get_campaign(created[0].id)).times == ["08:00", "20:00"]
    assert (await app.repo.get_campaign(campaign.id)).times == ["08:00", "20:00"]


async def test_apply_draft_from_chat_screen(feed, app):
    chat = await add_chat(feed, app, -101100)
    draft = await app.repo.create_campaign(None, "Шаблон")
    await app.repo.add_post(draft.id, kind="text", payload={"text": "x"})
    await feed(press(ApplyDraft(chat=chat.id, draft=draft.id)))
    await feed(press(ApplyDraft(chat=chat.id, draft=draft.id)))  # повторно — без дубля
    assert len(await app.repo.list_campaigns(chat.id)) == 1


async def test_count_helper_sets_even_times(feed, app):
    chat = await add_chat(feed, app, -101200)
    campaign = await app.repo.create_campaign(chat.id, "R")
    await feed(press(SchedAct(a="n", id=campaign.id, v=4)), press(SchedAct(a="w", id=campaign.id, v=4, w=0)))
    assert (await app.repo.get_campaign(campaign.id)).times == ["09:00", "13:00", "17:00", "21:00"]


async def test_timezone_change_reschedules(feed, app):
    chat = await add_chat(feed, app, -101300)
    campaign = await app.repo.create_campaign(chat.id, "R")
    await app.repo.add_post(campaign.id, kind="text", payload={"text": "x"})
    await app.repo.update_campaign(campaign.id, times=["12:00"], is_active=True)
    await app.scheduler.reschedule([campaign.id])
    before = (await app.repo.get_campaign(campaign.id)).next_run_ts
    await feed(press(SetAct(a="tz", v=22)))  # UTC
    assert app.settings.timezone == "UTC"
    after = (await app.repo.get_campaign(campaign.id)).next_run_ts
    assert after - before == 3 * 3600


# --------------------------------------------------------------------- навигация


async def test_nav_to_deleted_item_falls_back_to_menu(feed, session):
    await feed(press(Nav(to="camp", id=999)))
    assert session.calls(AnswerCallbackQuery)[-1].text == "Это уже удалено"
    assert "Автопостинг" in session.calls(EditMessageText)[-1].text


async def test_unknown_text_shows_hint(feed, session):
    await feed(private_message("привет"))
    assert any("Не понял" in text for text in owner_texts(session))


async def test_cancel_clears_input(feed, dp, bot, app):
    chat = await add_chat(feed, app, -101400)
    campaign = await app.repo.create_campaign(chat.id, "R")
    await feed(press(CampAct(a="rename", id=campaign.id)))
    assert await state_of(dp, bot) == Input.rename.state
    await feed(private_message("/cancel"))
    assert await state_of(dp, bot) is None


def test_callback_data_fits_telegram_limit():
    big = 2**31 - 1
    samples = [
        Nav(to="settings", id=big, page=big),
        ChatAct(a="delleave", id=big),
        CampAct(a="sync_ok", id=big),
        PostAct(a="btndel", id=big),
        SchedAct(a="clrstart", id=big, v=big, w=big),
        OptAct(a="clrthread", id=big),
        ApplyDraft(chat=big, draft=big),
        PickAct(a="go", v=big),
        SetAct(a="backup", v=big),
    ]
    for sample in samples:
        assert len(sample.pack().encode()) <= 64, sample
