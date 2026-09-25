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

from bot.db.repo import DRAFT
from bot.services.scheduler import Scheduler
from bot.setup import build_dispatcher
from bot.states import Input
from bot.storage import LeanMemoryStorage
from bot.ui.callbacks import (
    ApplyDraft,
    CampAct,
    ChatAct,
    LibAct,
    Nav,
    OptAct,
    PickAct,
    PostAct,
    SchedAct,
    SetAct,
    TargetAct,
)
from tests.conftest import BOT_ID, OWNER_ID, SECOND_ADMIN_ID, STRANGER_ID, FakeClock, admin_member, left_member

OWNER = User(id=OWNER_ID, is_bot=False, first_name="Owner")
SECOND = User(id=SECOND_ADMIN_ID, is_bot=False, first_name="Second")
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


def last_markup(session) -> Any:
    """Inline-клавиатура последнего экрана у владельца."""
    for request in reversed(session.requests):
        if (
            isinstance(request, (SendMessage, EditMessageText))
            and request.chat_id == OWNER_ID
            and hasattr(request.reply_markup, "inline_keyboard")
        ):
            return request.reply_markup
    return None


def button_data(markup: Any) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


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
        # защиту от двойных тапов проверяем отдельно — здесь кнопки жмутся подряд намеренно
        _DISPATCHER = build_dispatcher(app, album_latency=0.05, double_tap_window=0)
    _DISPATCHER["app"] = app
    _DISPATCHER.fsm.storage = LeanMemoryStorage()
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


async def test_second_admin_has_full_access(feed, app, session):
    await feed(private_message("/start", user=SECOND))
    texts = [r.text for r in session.requests if isinstance(r, SendMessage) and r.chat_id == SECOND_ADMIN_ID]
    assert any("Автопостинг" in text for text in texts)
    chat = await add_chat(feed, app, -100450, actor=SECOND)
    assert chat.status == "active"  # второй админ тоже «свой»


async def test_notifications_go_to_all_admins(feed, app, session):
    await add_chat(feed, app, -100460)
    recipients = {r.chat_id for r in session.calls(SendMessage) if "Бот добавлен" in r.text}
    assert recipients == {OWNER_ID, SECOND_ADMIN_ID}


async def test_preview_goes_to_requesting_admin(feed, app, session):
    chat = await add_chat(feed, app, -100470)
    campaign = await app.repo.create_campaign("R", chat_ids=[chat.id])
    await app.repo.add_post(campaign.id, kind="text", payload={"text": "Пост для второго"})
    await feed(press(CampAct(a="preview", id=campaign.id), user=SECOND))
    previews = [r.chat_id for r in session.calls(SendMessage) if r.text == "Пост для второго"]
    assert previews == [SECOND_ADMIN_ID]


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


async def test_bot_removed_keeps_campaign_and_notifies(feed, app, session):
    chat = await add_chat(feed, app, -100650)
    campaign = await app.repo.create_campaign("R", chat_ids=[chat.id])
    await app.repo.update_campaign(campaign.id, is_active=True, next_run_ts=1, next_slot_ts=1)
    await feed(membership(-100650, actor=STRANGER, new=left_member()))
    assert (await app.repo.get_chat(chat.id)).status == "left"
    assert (await app.repo.get_campaign(campaign.id)).is_active is True  # чат просто пропускается
    assert "потерял доступ" in owner_texts(session)[-1]
    await feed(membership(-100650))  # бота вернули
    assert "снова публикуют" in owner_texts(session)[-1]
    assert (await app.repo.campaign_targets(campaign.id))[0].deliverable


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

    await feed(press(OptAct(a="silent", id=campaign.id, v=1)), press(CampAct(a="on", id=campaign.id)))
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
    campaign = await app.repo.create_campaign("R", chat_ids=[chat.id])
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
    campaign = await app.repo.create_campaign("R", chat_ids=[chat.id])
    await feed(press(CampAct(a="add", id=campaign.id)))
    await feed(private_message(dice={"emoji": "🎲", "value": 3}))
    assert await app.repo.list_posts(campaign.id) == []
    assert "не поддерживается" in owner_texts(session)[-1]


async def test_buttons_flow_and_validation(feed, dp, bot, app, session):
    chat = await add_chat(feed, app, -100900)
    campaign = await app.repo.create_campaign("R", chat_ids=[chat.id])
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
    campaign = await app.repo.create_campaign("Реклама", chat_ids=[chat_a.id])
    await app.repo.add_post(campaign.id, kind="text", payload={"text": "Пост"})
    await app.repo.update_campaign(campaign.id, times=["12:00"])

    await feed(press(CampAct(a="todraft", id=campaign.id)))
    draft = (await app.repo.list_drafts())[0]
    await feed(
        press(CampAct(a="apply", id=draft.id)),
        press(PickAct(a="t", s=draft.id, v=chat_b.id)),
        press(PickAct(a="go", s=draft.id, v=1)),
    )
    # Одна общая рассылка: чат B добавился к рассылке, из которой сохранили черновик
    assert [c.id for c in await app.repo.list_campaigns()] == [campaign.id]
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.is_active and fresh.next_run_ts and fresh.source_draft_id == draft.id
    assert {t.chat.id for t in await app.repo.campaign_targets(campaign.id)} == {chat_a.id, chat_b.id}
    assert [c.id for c in await app.repo.linked_campaigns(draft.id)] == [campaign.id]

    # Правим черновик и обновляем рассылки из него
    await app.repo.update_campaign(draft.id, times=["08:00", "20:00"])
    await feed(press(CampAct(a="sync_ok", id=draft.id)))
    assert (await app.repo.get_campaign(campaign.id)).times == ["08:00", "20:00"]
    assert len(await app.repo.campaign_targets(campaign.id)) == 2


async def test_stale_picker_buttons_do_not_apply(feed, app, session):
    chat = await add_chat(feed, app, -101050)
    draft_a = await app.repo.create_campaign("A", kind=DRAFT)
    draft_b = await app.repo.create_campaign("B", kind=DRAFT)
    for draft in (draft_a, draft_b):
        await app.repo.add_post(draft.id, kind="text", payload={"text": "x"})
    await feed(press(CampAct(a="apply", id=draft_a.id)), press(PickAct(a="t", s=draft_a.id, v=chat.id)))
    await feed(press(CampAct(a="apply", id=draft_b.id)))  # открыли выбор для другого черновика
    await feed(press(PickAct(a="go", s=draft_a.id, v=0)))  # кнопка из старого окна
    assert await app.repo.list_campaigns(chat.id) == []
    assert "устарело" in session.calls(AnswerCallbackQuery)[-1].text


async def test_apply_draft_from_chat_screen(feed, app):
    chat = await add_chat(feed, app, -101100)
    other = await add_chat(feed, app, -101101)
    draft = await app.repo.create_campaign("Шаблон", kind=DRAFT)
    await app.repo.add_post(draft.id, kind="text", payload={"text": "x"})
    await feed(press(ApplyDraft(chat=chat.id, draft=draft.id)))
    await feed(press(ApplyDraft(chat=chat.id, draft=draft.id)))  # повторно — без дубля
    await feed(press(ApplyDraft(chat=other.id, draft=draft.id)))  # другой чат — в ту же рассылку
    campaigns = await app.repo.list_campaigns()
    assert len(campaigns) == 1 and not campaigns[0].is_active
    assert {t.chat.id for t in await app.repo.campaign_targets(campaigns[0].id)} == {chat.id, other.id}


async def test_count_helper_sets_even_times(feed, app):
    chat = await add_chat(feed, app, -101200)
    campaign = await app.repo.create_campaign("R", chat_ids=[chat.id])
    await feed(press(SchedAct(a="n", id=campaign.id, v=4)), press(SchedAct(a="w", id=campaign.id, v=4, w=0)))
    assert (await app.repo.get_campaign(campaign.id)).times == ["09:00", "13:00", "17:00", "21:00"]


async def test_timezone_change_reschedules(feed, app):
    chat = await add_chat(feed, app, -101300)
    campaign = await app.repo.create_campaign("R", chat_ids=[chat.id])
    await app.repo.add_post(campaign.id, kind="text", payload={"text": "x"})
    await app.repo.update_campaign(campaign.id, times=["12:00"], is_active=True)
    await app.scheduler.reschedule([campaign.id])
    before = (await app.repo.get_campaign(campaign.id)).next_run_ts
    await feed(press(SetAct(a="tz", v=22)))  # UTC
    assert app.settings.timezone == "UTC"
    after = (await app.repo.get_campaign(campaign.id)).next_run_ts
    assert after - before == 3 * 3600


# ----------------------------------------------------------------- чаты рассылки


async def test_main_menu_has_campaigns_and_posts(feed, session):
    await feed(private_message("/start"))
    data = button_data(last_markup(session))
    assert Nav(to="camps").pack() in data and Nav(to="lib").pack() in data


async def test_new_campaign_from_list_and_checkboxes(feed, app, session):
    chat_a = await add_chat(feed, app, -102001)
    chat_b = await add_chat(feed, app, -102002)
    await feed(press(Nav(to="camps")), press(CampAct(a="new", id=0)))
    campaign = (await app.repo.list_campaigns())[0]
    assert await app.repo.campaign_targets(campaign.id) == []
    assert "Чаты рассылки" in owner_texts(session)[-1]

    await feed(
        press(TargetAct(a="on", id=campaign.id, v=chat_a.id)),
        press(TargetAct(a="on", id=campaign.id, v=chat_b.id)),
    )
    assert {t.chat.id for t in await app.repo.campaign_targets(campaign.id)} == {chat_a.id, chat_b.id}
    assert "Выбрано: <b>2</b>" in owner_texts(session)[-1]
    # Значение приходит в кнопке: повторное «снять» не отмечает чат обратно
    off = TargetAct(a="off", id=campaign.id, v=chat_a.id)
    await feed(press(off), press(off))
    assert [t.chat.id for t in await app.repo.campaign_targets(campaign.id)] == [chat_b.id]

    await feed(press(TargetAct(a="none", id=campaign.id)))
    assert await app.repo.campaign_targets(campaign.id) == []
    await feed(press(TargetAct(a="all", id=campaign.id)))
    assert len(await app.repo.campaign_targets(campaign.id)) == 2


async def test_left_chat_cannot_be_checked(feed, app, session):
    chat = await add_chat(feed, app, -102050)
    await feed(membership(-102050, actor=STRANGER, new=left_member()))
    campaign = await app.repo.create_campaign("R")
    await feed(press(TargetAct(a="on", id=campaign.id, v=chat.id)))
    assert await app.repo.campaign_targets(campaign.id) == []
    assert session.calls(AnswerCallbackQuery)[-1].show_alert


async def test_campaign_from_chat_has_chat_and_back_button(feed, app, session):
    chat = await add_chat(feed, app, -102101)
    await feed(press(ChatAct(a="new", id=chat.id)))
    campaign = (await app.repo.list_campaigns(chat.id))[0]
    data = button_data(last_markup(session))
    assert Nav(to="chat", id=chat.id).pack() in data  # «« К чату»
    assert Nav(to="tgt", id=campaign.id).pack() in data  # «💬 Чаты (1)»
    await feed(press(CampAct(a="off", id=campaign.id)))  # действие на экране не теряет, откуда пришли
    assert Nav(to="chat", id=chat.id).pack() in button_data(last_markup(session))
    # Из списка рассылок «Назад» ведёт в список
    await feed(press(Nav(to="camp", id=campaign.id, f=-1)))
    assert Nav(to="camps").pack() in button_data(last_markup(session))


async def test_start_requires_checked_chat(feed, app, session):
    campaign = await app.repo.create_campaign("R")
    await app.repo.add_post(campaign.id, kind="text", payload={"text": "x"})
    await app.repo.update_campaign(campaign.id, times=["12:00"])
    await feed(press(CampAct(a="on", id=campaign.id)))
    answer = session.calls(AnswerCallbackQuery)[-1]
    assert answer.show_alert and "Отметьте чаты" in answer.text
    assert not (await app.repo.get_campaign(campaign.id)).is_active


async def test_forum_topic_is_set_per_chat(feed, dp, bot, app):
    chat = await add_chat(feed, app, -102201)
    campaign = await app.repo.create_campaign("R", chat_ids=[chat.id])
    await feed(press(TargetAct(a="thr", id=campaign.id, v=chat.id)))
    assert await state_of(dp, bot) == Input.thread.state
    await feed(private_message("https://t.me/c/1234567890/45"))
    assert (await app.repo.campaign_targets(campaign.id))[0].link.thread_id == 45
    assert await state_of(dp, bot) is None
    await feed(press(TargetAct(a="clrthr", id=campaign.id, v=chat.id)))
    assert (await app.repo.campaign_targets(campaign.id))[0].link.thread_id is None


async def test_resume_paused_chat(feed, app, session):
    chat = await add_chat(feed, app, -102301)
    campaign = await app.repo.create_campaign("R", chat_ids=[chat.id])
    await app.repo.update_target(campaign.id, chat.id, paused=True, fail_count=5, last_error="boom")
    await feed(press(Nav(to="tgt", id=campaign.id)))
    assert TargetAct(a="resume", id=campaign.id, v=chat.id).pack() in button_data(last_markup(session))
    await feed(press(TargetAct(a="resume", id=campaign.id, v=chat.id)))
    target = (await app.repo.campaign_targets(campaign.id))[0]
    assert target.deliverable and target.link.fail_count == 0
    # То же — с экрана чата, для всех его рассылок сразу
    await app.repo.update_target(campaign.id, chat.id, paused=True)
    await feed(press(ChatAct(a="resume", id=chat.id)))
    assert (await app.repo.campaign_targets(campaign.id))[0].deliverable


# ---------------------------------------------------------------------- мои посты


async def test_library_lists_posts_and_keeps_origin(feed, app, session):
    first = await app.repo.create_campaign("Первая")
    draft = await app.repo.create_campaign("Шаблон", kind=DRAFT)
    await app.repo.add_post(first.id, kind="text", payload={"text": "Старый пост"})
    post, _ = await app.repo.add_post(draft.id, kind="text", payload={"text": "Новый пост"})
    await app.repo.add_post(draft.id, kind="text", payload={"text": "Второй в черновике"})
    await feed(press(Nav(to="lib")))
    text = owner_texts(session)[-1]
    assert "Мои посты" in text and text.index("Новый пост") < text.index("Старый пост")
    await feed(press(Nav(to="post", id=post.id, f=1)))
    data = button_data(last_markup(session))
    assert Nav(to="lib").pack() in data  # назад — в «Мои посты»
    assert LibAct(a="new", id=post.id, f=1).pack() in data
    await feed(press(PostAct(a="down", id=post.id, f=1)))  # действие на карточке не теряет, откуда пришли
    assert Nav(to="lib").pack() in button_data(last_markup(session))


async def test_new_campaign_from_post(feed, app, session):
    source = await app.repo.create_campaign("Первая")
    buttons = [[{"text": "a", "url": "https://a.ru"}]]
    post, _ = await app.repo.add_post(source.id, kind="text", payload={"text": "Пост"}, buttons=buttons)
    await feed(press(LibAct(a="new", id=post.id, f=1)))
    campaigns = await app.repo.list_campaigns()
    assert len(campaigns) == 2
    posts = await app.repo.list_posts(campaigns[-1].id)
    assert [p.payload["text"] for p in posts] == ["Пост"] and posts[0].buttons == buttons
    assert "Чаты рассылки" in owner_texts(session)[-1]
    assert len(await app.repo.list_posts(source.id)) == 1  # исходный пост на месте


async def test_copy_post_to_other_campaign(feed, app, session):
    source = await app.repo.create_campaign("Первая")
    target = await app.repo.create_campaign("Вторая")
    await app.repo.add_post(target.id, kind="text", payload={"text": "уже был"})
    post, _ = await app.repo.add_post(source.id, kind="text", payload={"text": "Пост"})
    await feed(press(LibAct(a="to", id=post.id, f=1)))
    data = button_data(last_markup(session))
    assert LibAct(a="cp", id=post.id, v=target.id, f=1).pack() in data
    assert LibAct(a="cp", id=post.id, v=source.id, f=1).pack() not in data  # в свою же рассылку не предлагаем
    await feed(press(LibAct(a="cp", id=post.id, v=target.id, f=1)))
    assert [p.payload["text"] for p in await app.repo.list_posts(target.id)] == ["уже был", "Пост"]
    assert "пост #2" in owner_texts(session)[-1]


async def test_new_post_from_library(feed, dp, bot, app, session):
    await feed(press(Nav(to="lib")), press(LibAct(a="add", id=0)))
    assert await state_of(dp, bot) == Input.posts.state
    campaign = (await app.repo.list_campaigns())[0]
    await feed(private_message("Свежий пост"))
    reply = [c for c in session.calls(SendMessage) if c.text.startswith("✅ Пост #1")][-1]
    assert reply.reply_markup.inline_keyboard[0][0].callback_data == CampAct(a="fin", id=campaign.id).pack()
    await feed(press(CampAct(a="fin", id=campaign.id)))
    assert await state_of(dp, bot) is None
    assert [p.payload["text"] for p in await app.repo.list_posts(campaign.id)] == ["Свежий пост"]
    assert "отметьте чаты" in owner_texts(session)[-1]


async def test_cancel_new_post_removes_empty_campaign(feed, app, session):
    await feed(press(LibAct(a="add", id=0)))
    campaign = (await app.repo.list_campaigns())[0]
    await feed(press(LibAct(a="drop", id=campaign.id)))
    assert await app.repo.list_campaigns() == []
    assert "Мои посты" in owner_texts(session)[-1]
    # «Готово» без единого поста — тоже без пустой рассылки
    await feed(press(LibAct(a="add", id=0)))
    campaign = (await app.repo.list_campaigns())[0]
    await feed(press(CampAct(a="fin", id=campaign.id)))
    assert await app.repo.list_campaigns() == []


async def test_delete_post_from_library_returns_to_library(feed, app, session):
    campaign = await app.repo.create_campaign("R")
    post, _ = await app.repo.add_post(campaign.id, kind="text", payload={"text": "x"})
    await feed(press(PostAct(a="del", id=post.id, f=1)), press(PostAct(a="del_ok", id=post.id, f=1)))
    assert await app.repo.get_post(post.id) is None
    assert "Мои посты" in owner_texts(session)[-1]


# --------------------------------------------------------------------- навигация


async def test_nav_to_deleted_item_falls_back_to_menu(feed, session):
    await feed(press(Nav(to="camp", id=999)))
    assert session.calls(AnswerCallbackQuery)[-1].text == "Это уже удалено"
    assert "Автопостинг" in session.calls(EditMessageText)[-1].text


async def test_buttons_from_previous_version_still_work(feed, app, session):
    """Панели и уведомления, отправленные до обновления, несут кнопки без новых полей."""
    await feed(press("n:chats:0:0"))
    assert "Мои чаты" in owner_texts(session)[-1]
    campaign = await app.repo.create_campaign("Старая")
    post, _ = await app.repo.add_post(campaign.id, kind="text", payload={"text": "x"})
    await feed(press(f"n:camp:{campaign.id}:0"), press(f"p:del:{post.id}"))
    assert "Удалить пост" in owner_texts(session)[-1]


async def test_unknown_text_shows_hint(feed, session):
    await feed(private_message("привет"))
    assert any("Не понял" in text for text in owner_texts(session))


async def test_cancel_clears_input(feed, dp, bot, app):
    chat = await add_chat(feed, app, -101400)
    campaign = await app.repo.create_campaign("R", chat_ids=[chat.id])
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
        PickAct(a="none", s=big, v=big),
        SetAct(a="backup", v=big),
        TargetAct(a="resume", id=big, v=big, p=big),
        LibAct(a="cp", id=big, v=big, f=big),
        Nav(to="upcoming", id=big, page=big, f=-big),
        PostAct(a="btndel", id=big, f=big),
    ]
    for sample in samples:
        assert len(sample.pack().encode()) <= 64, sample
