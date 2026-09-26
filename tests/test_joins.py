"""Автоприём заявок на вступление через настоящий диспетчер."""

# Фикстуры диспетчера берутся из test_flows — pytest передаёт их в тесты по имени
# ruff: noqa: F811

from __future__ import annotations

import itertools
from datetime import datetime

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, ApproveChatJoinRequest, SendMessage
from aiogram.types import Chat, ChatJoinRequest, Update, User

from bot.services import moderation as moderation_module
from bot.services.moderation import Moderator
from bot.ui import keyboards
from bot.ui.callbacks import Guard, Nav
from tests.conftest import OWNER_ID, STRANGER_ID, admin_member
from tests.test_flows import (  # noqa: F401
    add_chat,
    button_data,
    clock,
    dp,
    feed,
    last_markup,
    owner_texts,
    press,
)

GROUP = -1005000000001
CHANNEL = -1005000000002
OTHER = -1005000000003
PERSON = User(id=5001, is_bot=False, first_name="Анна")
_ids = itertools.count(1)


@pytest.fixture
async def moderator(app, clock):
    instance = Moderator(app, wall=clock)  # те же часы, что у экранов: «сегодня» совпадает
    app.moderator = instance
    yield instance
    await instance.close()


def join_request(chat_id: int, user: User = PERSON, chat_type: str = "supergroup") -> Update:
    request = ChatJoinRequest(
        chat=Chat(id=chat_id, type=chat_type, title="Чат"),
        from_user=user,
        user_chat_id=user.id,
        date=datetime.now(),
    )
    return Update(update_id=next(_ids), chat_join_request=request)


def approved(session) -> list[tuple[int, int]]:
    return [(int(call.chat_id), call.user_id) for call in session.calls(ApproveChatJoinRequest)]


def refusal(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=ApproveChatJoinRequest(chat_id=GROUP, user_id=PERSON.id), message=message)


async def add_with_invite(feed, app, chat_id: int, **kwargs):
    """Чат, где у бота есть право приглашать — только тогда Telegram присылает ему заявки."""
    return await add_chat(feed, app, chat_id, new=admin_member(can_invite_users=True), **kwargs)


def owner_warnings(session, needle: str) -> list[SendMessage]:
    return [c for c in session.calls(SendMessage) if c.chat_id == OWNER_ID and needle in c.text]


# ------------------------------------------------------------------------ приём


async def test_requests_are_approved_in_groups_and_channels(feed, app, session, moderator):
    group = await add_with_invite(feed, app, GROUP)
    channel = await add_with_invite(feed, app, CHANNEL, chat_type="channel")
    session.clear()
    await feed(join_request(GROUP), join_request(CHANNEL, chat_type="channel"))
    assert approved(session) == [(GROUP, PERSON.id), (CHANNEL, PERSON.id)]
    assert await app.repo.join_counts(0) == 2
    assert await app.repo.join_counts(0, group.id) == 1
    # принятые заявки — не удалённый спам: в счётчики и журнал защиты не попадают
    assert await app.repo.moderation_counts(0) == (0, 0)
    assert await app.repo.last_moderation(channel.id) == []


async def test_chat_switched_off_leaves_requests_to_admins(feed, app, session, moderator):
    group = await add_with_invite(feed, app, GROUP)
    await feed(press(Guard(a="jchat", id=group.id, v=0)))
    assert (await app.repo.get_chat(group.id)).auto_approve is False
    session.clear()
    await feed(join_request(GROUP))
    assert approved(session) == []

    await feed(press(Guard(a="jchat", id=group.id, v=1)), join_request(GROUP))
    assert approved(session) == [(GROUP, PERSON.id)]


async def test_everywhere_switch_sets_default_for_all_and_new_chats(feed, app, session, moderator):
    first = await add_with_invite(feed, app, GROUP)
    second = await add_with_invite(feed, app, CHANNEL, chat_type="channel")
    await feed(press(Guard(a="jn", id=first.id, v=1)))  # выбран отдельно
    await feed(press(Guard(a="jall", v=0)))
    assert app.settings.join_auto is False
    assert (await app.repo.load_settings())["join_auto"] == "0"
    assert (await app.repo.get_chat(first.id)).auto_approve is None  # «во всех» сбрасывает выбранное отдельно
    assert "выключен во всех" in owner_texts(session)[-1]
    session.clear()
    await feed(join_request(GROUP), join_request(CHANNEL, chat_type="channel"))
    assert approved(session) == []

    await feed(press(Guard(a="jn", id=second.id, v=1)), join_request(CHANNEL, chat_type="channel"))
    assert approved(session) == [(CHANNEL, PERSON.id)]  # включённый отдельно чат принимает

    await add_with_invite(feed, app, OTHER)
    await feed(join_request(OTHER))
    assert (OTHER, PERSON.id) not in approved(session)  # новый чат — по общей настройке
    await feed(press(Guard(a="jall", v=1)), join_request(OTHER))
    assert (OTHER, PERSON.id) in approved(session)


async def test_unknown_or_unconfirmed_chat_is_left_alone(feed, app, session, moderator):
    stranger = User(id=STRANGER_ID, is_bot=False, first_name="Чужой")
    await add_chat(feed, app, GROUP, actor=stranger, new=admin_member(can_invite_users=True))
    assert (await app.repo.get_chat_by_tg(GROUP)).status == "pending"  # бота добавил не админ
    session.clear()
    await feed(join_request(GROUP), join_request(-1009999999999))
    assert approved(session) == []


async def test_flood_limit_is_waited_out(feed, app, session, moderator, monkeypatch):
    await add_with_invite(feed, app, GROUP)
    waits: list[float] = []

    async def no_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(moderation_module.asyncio, "sleep", no_sleep)
    session.clear()
    method = ApproveChatJoinRequest(chat_id=GROUP, user_id=PERSON.id)
    session.queue(ApproveChatJoinRequest, TelegramRetryAfter(method=method, message="Too Many Requests", retry_after=7))
    await feed(join_request(GROUP))
    assert approved(session) == [(GROUP, PERSON.id)] * 2  # первая попытка упёрлась в лимит, вторая прошла
    assert waits == [7]
    assert await app.repo.join_counts(0) == 1


async def test_request_handled_elsewhere_is_skipped_quietly(feed, app, session, moderator):
    await add_with_invite(feed, app, GROUP)
    session.clear()
    session.queue(ApproveChatJoinRequest, refusal("Bad Request: HIDE_REQUESTER_MISSING"))
    await feed(join_request(GROUP))
    assert await app.repo.join_counts(0) == 0
    assert [c for c in session.calls(SendMessage) if c.chat_id == OWNER_ID] == []


async def test_missing_right_is_reported_once_a_day(feed, app, session, moderator):
    await add_with_invite(feed, app, GROUP)
    session.clear()
    for _ in range(2):
        session.queue(ApproveChatJoinRequest, refusal("Bad Request: not enough rights to manage join requests"))
    await feed(join_request(GROUP), join_request(GROUP, user=User(id=5002, is_bot=False, first_name="Борис")))
    (warning,) = owner_warnings(session, "Автоприём заявок")
    assert "«Добавление участников»" in warning.text
    assert await app.repo.join_counts(0) == 0


# ------------------------------------------------------------------------ экраны


async def test_joins_screen_checkboxes_and_warnings(feed, app, session, moderator):
    group = await add_with_invite(feed, app, GROUP)
    channel = await add_chat(feed, app, CHANNEL, chat_type="channel")  # без права приглашать
    await feed(press(Nav(to="settings")))
    assert Guard(a="joins").pack() in button_data(last_markup(session))
    assert "Автоприём заявок: включён в 2 из 2 чатов" in owner_texts(session)[-1]

    await feed(press(Guard(a="joins")))
    text = owner_texts(session)[-1]
    assert "Включён: <b>2</b> из 2" in text and "Нет права приглашать" in text
    data = button_data(last_markup(session))
    assert Guard(a="jn", id=group.id, v=0).pack() in data
    assert Guard(a="jn", id=channel.id, v=0).pack() in data

    await feed(press(Guard(a="jn", id=channel.id, v=0)))
    assert (await app.repo.get_chat(channel.id)).auto_approve is False
    assert Guard(a="jn", id=channel.id, v=1).pack() in button_data(last_markup(session))
    text = owner_texts(session)[-1]
    assert "Включён: <b>1</b> из 2" in text and "Нет права приглашать" not in text


async def test_chat_screen_shows_right_and_toggle(feed, app, session, moderator):
    channel = await add_chat(feed, app, CHANNEL, chat_type="channel")  # без права приглашать
    await feed(press(Nav(to="chat", id=channel.id)))
    text = owner_texts(session)[-1]
    assert "❌ приглашения" in text and "Автоприём заявок: ✅ включён" in text
    assert "«Добавление подписчиков»" in text  # в канале право называется так

    await feed(press(Guard(a="jchat", id=channel.id, v=0)))
    assert session.calls(AnswerCallbackQuery)[-1].text.startswith("⏸")
    assert "Автоприём заявок: ⏸ выключен" in owner_texts(session)[-1]

    await feed(press(Guard(a="jchat", id=channel.id, v=1)))
    answer = session.calls(AnswerCallbackQuery)[-1]
    assert answer.show_alert and "«Добавление подписчиков»" in answer.text  # включили, но права нет


async def test_accepted_requests_are_counted_on_screens(feed, app, session, moderator):
    group = await add_with_invite(feed, app, GROUP)
    await feed(join_request(GROUP), press(Nav(to="main")))
    assert "Сегодня принято заявок: 1" in owner_texts(session)[-1]
    await feed(press(Nav(to="chat", id=group.id)))
    assert "✅ приглашения" in owner_texts(session)[-1]
    assert "сегодня принято 1" in owner_texts(session)[-1]
    await feed(press(Guard(a="joins")))
    assert "Принято сегодня: 1 · за 7 дней: 1" in owner_texts(session)[-1]


# ------------------------------------------------------------------------ прочее


def test_adding_group_asks_for_invite_right():
    reply = keyboards.add_chat_reply()
    assert reply.keyboard[0][0].request_chat.bot_administrator_rights.can_invite_users is True
    assert "invite_users" in keyboards.add_group_link("autopost_bot")


def test_bot_subscribes_to_join_requests(dp):
    assert "chat_join_request" in dp.resolve_used_update_types()


async def test_join_requests_do_not_grow_fsm_storage(feed, dp, app, moderator):
    await add_with_invite(feed, app, GROUP)
    for n in range(20):
        await feed(join_request(GROUP, user=User(id=60_000 + n, is_bot=False, first_name="U")))
    assert len(dp.fsm.storage.storage) == 0
