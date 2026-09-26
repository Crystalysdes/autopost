"""Команды админов в группах через настоящий диспетчер: /ban, /mute, /del и другие."""

# Фикстуры диспетчера берутся из test_flows — pytest передаёт их в тесты по имени
# ruff: noqa: F811

from __future__ import annotations

import asyncio
import itertools
from datetime import datetime
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    BanChatMember,
    BanChatSenderChat,
    GetChatAdministrators,
    RestrictChatMember,
    SendMessage,
    SetMyCommands,
    UnbanChatMember,
)
from aiogram.types import (
    BotCommandScopeAllChatAdministrators,
    Chat,
    ForumTopicCreated,
    Message,
    MessageEntity,
    Update,
    User,
)

from bot.handlers.common import setup_commands
from bot.services import trusted
from bot.services.group_commands import parse_duration
from bot.services.moderation import Moderator
from bot.ui.callbacks import Nav
from tests.conftest import BOT_ID, OWNER_ID, admin_member
from tests.test_flows import add_chat, clock, dp, feed, owner_texts, press  # noqa: F401
from tests.test_moderation import GROUP, deleted, group_sends

ADMIN = User(id=8001, is_bot=False, first_name="Модератор")
VIOLATOR = User(id=8002, is_bot=False, first_name="Нарушитель")
OTHER_ADMIN = User(id=8003, is_bot=False, first_name="Другой админ")
MANAGER = User(id=8004, is_bot=False, first_name="Скрытый", username="hidden_one")
OWNER = User(id=OWNER_ID, is_bot=False, first_name="Владелец")
BOT_USER = User(id=BOT_ID, is_bot=True, first_name="Autopost")
CHANNEL_BOT = User(id=136817688, is_bot=True, first_name="Channel")
SPAM_CHANNEL = Chat(id=-1001000000777, type="channel", title="Спам-канал")
_ids = itertools.count(50_000)


@pytest.fixture
async def moderator(app, clock):
    instance = Moderator(app, clock=clock, wall=clock)
    instance.hint_lifetime = 0  # подсказки исчезают сразу — тесты не ждут
    app.moderator = instance
    yield instance
    await instance.close()


@pytest.fixture
async def group(feed, app, moderator):
    return await add_chat(feed, app, GROUP)


def chat_admin(user: User, **rights: bool):
    return admin_member(**rights).model_copy(update={"user": user})


def admins(session, *members) -> None:
    session.handlers[GetChatAdministrators] = lambda method: list(members)


def target(user: User = VIOLATOR, **fields: Any) -> Message:
    fields.setdefault("text", "спам")
    return Message(
        message_id=next(_ids),
        date=datetime.now(),
        chat=Chat(id=GROUP, type="supergroup", title="Группа"),
        from_user=user,
        **fields,
    )


def command(text: str, *, user: User = ADMIN, reply: Message | None = None, **fields: Any) -> Update:
    message = Message(
        message_id=next(_ids),
        date=datetime.now(),
        chat=Chat(id=GROUP, type="supergroup", title="Группа"),
        from_user=user,
        text=text,
        entities=[MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))],
        reply_to_message=reply,
        **fields,
    )
    return Update(update_id=next(_ids), message=message)


async def settle() -> None:
    """Дать отработать фоновым задачам (удаление подсказок)."""
    for _ in range(3):
        await asyncio.sleep(0)


def hints(session) -> list[str]:
    return [call.text for call in group_sends(session)]


# ------------------------------------------------------------------------ действия


async def test_mute_for_a_day_leaves_no_trace(feed, app, session, group, clock):
    admins(session, chat_admin(ADMIN, can_restrict_members=True))
    session.clear()
    reply = target()
    update = command("/mute 1day", reply=reply)
    await feed(update)
    (call,) = session.calls(RestrictChatMember)
    assert call.user_id == VIOLATOR.id and call.until_date == int(clock()) + 24 * 3600
    assert call.permissions.can_send_messages is False and call.permissions.can_send_photos is False
    assert call.use_independent_chat_permissions is True
    assert deleted(session) == [update.message.message_id]  # команда удалена, сообщение нарушителя — нет
    assert group_sends(session) == []  # в чат бот ничего не пишет


async def test_ban_forever_removes_service_message_even_with_rule_off(feed, app, session, group, moderator):
    await app.repo.update_chat(group.id, spam_filter={"service": False})
    moderator.forget()
    admins(session, chat_admin(ADMIN, can_restrict_members=True))
    await feed(command("/ban", reply=target()))
    (ban,) = session.calls(BanChatMember)
    assert ban.user_id == VIOLATOR.id and ban.until_date is None

    removed = Message(
        message_id=next(_ids),
        date=datetime.now(),
        chat=Chat(id=GROUP, type="supergroup", title="Группа"),
        from_user=BOT_USER,
        left_chat_member=VIOLATOR,
    )
    await feed(Update(update_id=next(_ids), message=removed))
    assert removed.message_id in deleted(session)  # «бот удалил X» — убрано


async def test_delete_and_combined_commands(feed, app, session, group, clock):
    admins(session, chat_admin(ADMIN, can_restrict_members=True, can_delete_messages=True))
    only_delete, to_ban, to_mute = target(), target(), target()
    await feed(command("/del", reply=only_delete))
    assert only_delete.message_id in deleted(session)
    assert session.calls(BanChatMember) == [] and session.calls(RestrictChatMember) == []

    await feed(command("/delban 2h", reply=to_ban), command("/delmute", reply=to_mute))
    assert {to_ban.message_id, to_mute.message_id} <= set(deleted(session))
    assert session.calls(BanChatMember)[0].until_date == int(clock()) + 2 * 3600
    assert session.calls(RestrictChatMember)[0].until_date is None  # без срока — навсегда


async def test_unmute_and_unban_by_reply_or_id(feed, app, session, group):
    admins(session, chat_admin(ADMIN, can_restrict_members=True))
    await feed(command("/unmute", reply=target()))
    (call,) = session.calls(RestrictChatMember)
    assert call.permissions.can_send_messages is True and call.permissions.can_send_polls is True
    await feed(command(f"/unban {VIOLATOR.id}"))
    (unban,) = session.calls(UnbanChatMember)
    assert unban.user_id == VIOLATOR.id and unban.only_if_banned is True


async def test_channel_can_be_banned_but_not_muted(feed, app, session, group):
    admins(session, chat_admin(ADMIN, can_restrict_members=True))
    await feed(command("/ban", reply=target(CHANNEL_BOT, sender_chat=SPAM_CHANNEL)))
    (ban,) = session.calls(BanChatSenderChat)
    assert ban.sender_chat_id == SPAM_CHANNEL.id
    await feed(command("/mute 1h", reply=target(CHANNEL_BOT, sender_chat=SPAM_CHANNEL)))
    assert session.calls(RestrictChatMember) == []
    assert "Канал нельзя замутить" in hints(session)[-1]


# ------------------------------------------------------------------------ кто может


async def test_strangers_commands_are_just_deleted(feed, app, session, group):
    session.clear()
    update = command("/ban", user=VIOLATOR, reply=target(OTHER_ADMIN))
    await feed(update)
    assert deleted(session) == [update.message.message_id]
    assert session.calls(BanChatMember) == [] and group_sends(session) == []


async def test_hidden_and_bot_admins_can_use_commands(feed, app, session, group, moderator):
    await app.settings.set_list(app.repo, "trusted", [trusted.from_user(MANAGER)])
    moderator.forget()
    await feed(command("/mute 30min", user=MANAGER, reply=target()))
    assert len(session.calls(RestrictChatMember)) == 1
    await feed(command("/ban", user=OWNER, reply=target()))
    assert len(session.calls(BanChatMember)) == 1


async def test_chat_admin_needs_the_right(feed, app, session, group):
    admins(session, chat_admin(ADMIN, can_restrict_members=False, can_delete_messages=True))
    reply = target()
    await feed(command("/ban", reply=reply))
    assert session.calls(BanChatMember) == []  # банить не может
    await feed(command("/del", reply=reply))
    assert reply.message_id in deleted(session)  # а удалять — может


async def test_newly_appointed_admin_is_picked_up(feed, app, session, group, clock):
    current: list[Any] = []
    session.handlers[GetChatAdministrators] = lambda method: list(current)
    await feed(command("/ban", reply=target()))
    assert session.calls(BanChatMember) == []
    current.append(chat_admin(ADMIN, can_restrict_members=True))
    clock.advance(61)  # список админов перечитывается не чаще раза в минуту
    await feed(command("/ban", reply=target()))
    assert len(session.calls(BanChatMember)) == 1


async def test_creator_and_anonymous_admin(feed, app, session, group):
    creator = chat_admin(ADMIN).model_copy(update={"status": "creator"})
    admins(session, creator)
    await feed(command("/ban", reply=target()))
    assert len(session.calls(BanChatMember)) == 1
    anonymous = command(
        "/mute 1h",
        user=User(id=1087968824, is_bot=True, first_name="Group"),
        reply=target(),
        sender_chat=Chat(id=GROUP, type="supergroup", title="Группа"),
    )
    await feed(anonymous)
    assert len(session.calls(RestrictChatMember)) == 1


# ------------------------------------------------------------------------ подсказки


async def test_protected_targets_get_a_vanishing_hint(feed, app, session, group):
    admins(session, chat_admin(ADMIN, can_restrict_members=True), chat_admin(OTHER_ADMIN))
    hint_ids = itertools.count(90_001)

    def numbered(method: SendMessage):
        if method.chat_id != GROUP:
            return session._default(method)
        return Message(message_id=next(hint_ids), date=datetime.now(), chat=Chat(id=GROUP, type="supergroup"))

    session.handlers[SendMessage] = numbered
    await feed(
        command("/ban", reply=target(OTHER_ADMIN)),
        command("/mute", reply=target(OWNER)),
        command("/ban", reply=target(BOT_USER)),
    )
    assert session.calls(BanChatMember) == [] and session.calls(RestrictChatMember) == []
    texts = hints(session)
    assert len(texts) == 3 and "админ" in texts[0] and "админ" in texts[1] and "Себя бот" in texts[2]
    await settle()
    assert {90_001, 90_002, 90_003} <= set(deleted(session))  # подсказки исчезли сами


async def test_no_reply_bad_duration_and_forum_topic(feed, app, session, group):
    admins(session, chat_admin(ADMIN, can_restrict_members=True))
    await feed(command("/mute"))
    assert "ответом на сообщение" in hints(session)[-1]
    await feed(command("/mute 1dya", reply=target()))
    assert "Не понял срок «1dya»" in hints(session)[-1]

    topic = target(forum_topic_created=ForumTopicCreated(name="Тема", icon_color=0x6FB9F0), text=None)
    await feed(command("/ban", reply=topic, message_thread_id=topic.message_id, is_topic_message=True))
    assert "ответом на сообщение" in hints(session)[-1]
    assert group_sends(session)[-1].message_thread_id == topic.message_id  # подсказка — в ту же тему
    assert session.calls(BanChatMember) == [] and session.calls(RestrictChatMember) == []


async def test_bot_without_restrict_right_gets_hint(feed, app, session, group):
    admins(session, chat_admin(ADMIN, can_restrict_members=True))
    refused = TelegramBadRequest(
        method=BanChatMember(chat_id=GROUP, user_id=VIOLATOR.id),
        message="Bad Request: not enough rights to restrict/unrestrict chat member",
    )
    session.queue(BanChatMember, refused)
    await feed(command("/ban", reply=target()))
    assert "нет права «Блокировка пользователей»" in hints(session)[-1]


# ------------------------------------------------------------------------ прочее


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ("", (None, None)),
        ("1min", (60, None)),
        ("1 min", (60, None)),
        ("30мин", (1800, None)),
        ("2h", (7200, None)),
        ("2 часа", (7200, None)),
        ("1day", (86400, None)),
        ("1д спам", (86400, None)),
        ("1week", (7 * 86400, None)),
        ("1mont", (30 * 86400, None)),
        ("1мес", (30 * 86400, None)),
        ("13mo", (None, None)),  # дольше 366 дней — навсегда
        ("спамер", (None, None)),  # это причина, не срок
        ("1dya", (None, "1dya")),
        ("10", (None, "10")),
        ("0min", (None, "0min")),
    ],
)
def test_parse_duration(args, expected):
    assert parse_duration(args) == expected


async def test_group_admins_get_command_menu(bot, session):
    await setup_commands(bot, [OWNER_ID])
    menus = [c for c in session.calls(SetMyCommands) if isinstance(c.scope, BotCommandScopeAllChatAdministrators)]
    assert {c.command for c in menus[0].commands} == {"del", "mute", "delmute", "ban", "delban", "unmute", "unban"}


async def test_chat_screen_shows_restrict_right(feed, app, session, moderator):
    chat = await add_chat(feed, app, GROUP, new=admin_member(can_restrict_members=True))
    assert chat.can_restrict is True
    await feed(press(Nav(to="chat", id=chat.id)))
    assert "✅ блокировка" in owner_texts(session)[-1]


async def test_sent_hint_is_not_a_command_trace(feed, app, session, group):
    """Успешная команда не оставляет в чате ни одного сообщения бота."""
    admins(session, chat_admin(ADMIN, can_restrict_members=True, can_delete_messages=True))
    await feed(command("/delmute 1h", reply=target()), command("/delban", reply=target()))
    assert [c for c in session.calls(SendMessage) if c.chat_id == GROUP] == []
