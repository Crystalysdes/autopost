"""Защита групп через настоящий диспетчер: антиспам и обязательная подписка."""

# Фикстуры диспетчера берутся из test_flows — pytest передаёт их в тесты по имени
# ruff: noqa: F811

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    DeleteMessages,
    GetChat,
    GetChatAdministrators,
    GetChatMember,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, ChatMemberLeft, ChatMemberMember, Message, Update, User

from bot.services.moderation import Moderator
from bot.ui.callbacks import Nav, SubCheck
from tests.conftest import BOT_ID, OWNER_ID, STRANGER_ID, admin_member, chat_info
from tests.test_flows import add_chat, clock, dp, feed  # noqa: F401

STRANGER = User(id=STRANGER_ID, is_bot=False, first_name="Спамер")
MEMBER = User(id=3000, is_bot=False, first_name="Участник")
GROUP = -1003000000001
CHANNEL = -1003000000999
_ids = itertools.count(10_000)


@pytest.fixture
def moderator(app):
    instance = Moderator(app)
    instance.notice_lifetime = 0  # подсказка удаляется сразу — тесты не ждут минуту
    app.moderator = instance
    yield instance


@pytest.fixture(autouse=True)
async def _close_moderator(app):
    yield
    if app.moderator is not None:
        await app.moderator.close()


def group_msg(chat_id: int = GROUP, *, user: User | None = STRANGER, update: str = "message", **fields: Any) -> Update:
    fields.setdefault("date", datetime.now())
    message = Message(
        message_id=next(_ids),
        chat=Chat(id=chat_id, type="supergroup", title="Группа"),
        from_user=user,
        **fields,
    )
    return Update(update_id=next(_ids), **{update: message})


def press_in_group(data: Any, user: User, chat_id: int = GROUP, message_id: int = 777) -> Update:
    notice = Message(
        message_id=message_id,
        date=datetime.now(),
        chat=Chat(id=chat_id, type="supergroup", title="Группа"),
        from_user=User(id=BOT_ID, is_bot=True, first_name="Autopost"),
        text="подсказка",
    )
    query = CallbackQuery(id=str(next(_ids)), from_user=user, chat_instance="g", message=notice, data=data.pack())
    return Update(update_id=next(_ids), callback_query=query)


def deleted(session, chat_id: int = GROUP) -> list[int]:
    ids = [call.message_id for call in session.calls(DeleteMessage) if call.chat_id == chat_id]
    for call in session.calls(DeleteMessages):
        if call.chat_id == chat_id:
            ids += call.message_ids
    return sorted(ids)


def group_sends(session, chat_id: int = GROUP) -> list[SendMessage]:
    return [call for call in session.calls(SendMessage) if call.chat_id == chat_id]


def chat_admin(user: User):
    return admin_member().model_copy(update={"user": user})


async def setup_group(feed, app, chat_id: int = GROUP):
    return await add_chat(feed, app, chat_id)


# ------------------------------------------------------------------------ антиспам


async def test_spam_from_member_is_deleted_silently(feed, app, session, moderator):
    chat = await setup_group(feed, app)
    session.clear()
    update = group_msg(text="Заходи на spam.site, заработок без вложений")
    await feed(update)
    assert deleted(session) == [update.message.message_id]
    assert group_sends(session) == []  # молча
    assert await app.repo.moderation_counts(0, chat.id) == (1, 0)
    log = (await app.repo.last_moderation(chat.id))[0]
    assert log.reason == "links" and log.user_id == STRANGER_ID and "spam.site" in log.snippet


async def test_normal_message_is_kept_without_api_calls(feed, app, session, moderator):
    await setup_group(feed, app)
    session.clear()
    await feed(group_msg(text="Всем привет! Во сколько встреча?"))
    assert session.requests == []


async def test_admins_are_not_checked(feed, app, session, moderator):
    await setup_group(feed, app)
    session.handlers[GetChatAdministrators] = lambda method: [chat_admin(STRANGER)]
    session.clear()
    await feed(group_msg(text="Наш сайт: shop.ru"))  # админ чата
    await feed(group_msg(user=User(id=OWNER_ID, is_bot=False, first_name="Owner"), text="t.me/x"))  # админ бота
    anonymous = Chat(id=GROUP, type="supergroup", title="Группа")
    await feed(
        group_msg(user=User(id=1087968824, is_bot=True, first_name="Group"), sender_chat=anonymous, text="site.ru")
    )
    assert deleted(session) == []


async def test_linked_channel_posts_are_kept(feed, app, session, moderator):
    await setup_group(feed, app)
    channel = Chat(id=CHANNEL, type="channel", title="Наш канал")
    origin = {"type": "channel", "date": datetime.now(), "chat": channel, "message_id": 5}
    service = User(id=777000, is_bot=False, first_name="Telegram")
    await feed(
        group_msg(user=service, sender_chat=channel, is_automatic_forward=True, forward_origin=origin, text="Пост")
    )
    session.handlers[GetChat] = lambda method: chat_info(int(method.chat_id), linked_chat_id=CHANNEL)
    channel_bot = User(id=136817688, is_bot=True, first_name="Channel")
    await feed(group_msg(user=channel_bot, sender_chat=channel, text="Комментарий от канала: site.ru"))
    assert deleted(session) == []
    # а чужой канал — спам
    other = Chat(id=-1009, type="channel", title="Чужой")
    spam = group_msg(user=channel_bot, sender_chat=other, text="Привет")
    await feed(spam)
    assert deleted(session) == [spam.message.message_id]


async def test_antispam_can_be_turned_off_in_chat(feed, app, session, moderator):
    chat = await setup_group(feed, app)
    await app.repo.update_chat(chat.id, spam_filter={"on": False})
    moderator.forget(GROUP)
    await feed(group_msg(text="spam.site"))
    assert deleted(session) == []


async def test_mentions_of_bots_and_channels(feed, app, session, moderator):
    await setup_group(feed, app)

    def get_chat(method):
        if method.chat_id == "@crypto_news":
            return chat_info(-1005555, title="Крипто", chat_type="channel")
        if isinstance(method.chat_id, str):
            raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
        return chat_info(int(method.chat_id))

    session.handlers[GetChat] = get_chat
    bot_mention = group_msg(text="Жми @earn_money_bot")
    channel_mention = group_msg(text="Все новости в @crypto_news")
    person = group_msg(text="@vasya_petrov, глянь")
    await feed(bot_mention, channel_mention, person)
    assert deleted(session) == sorted([bot_mention.message.message_id, channel_mention.message.message_id])
    await feed(group_msg(text="@vasya_petrov, ещё раз"))
    assert [c.chat_id for c in session.calls(GetChat)].count("@vasya_petrov") == 1  # ответ запомнен


async def test_spam_album_is_deleted_whole(feed, app, session, moderator):
    await setup_group(feed, app)
    photo = [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}]
    parts = [
        group_msg(media_group_id="alb", photo=photo),
        group_msg(media_group_id="alb", photo=photo, caption="Скидки тут: t.me/spam_shop"),
        group_msg(media_group_id="alb", photo=photo),
    ]
    await feed(*parts)
    assert deleted(session) == sorted(p.message.message_id for p in parts)


async def test_spam_added_by_edit_is_deleted(feed, app, session, moderator):
    await setup_group(feed, app)
    edit = group_msg(
        update="edited_message", text="Привет! Подробности: spam.site", edit_date=int(datetime.now().timestamp())
    )
    await feed(edit)
    assert deleted(session) == [edit.edited_message.message_id]


async def test_join_and_leave_messages_are_removed(feed, app, session, moderator):
    chat = await setup_group(feed, app)
    joined = group_msg(new_chat_members=[MEMBER])
    await feed(joined)
    assert deleted(session) == [joined.message.message_id]
    await app.repo.update_chat(chat.id, spam_filter={"service": False})
    moderator.forget(GROUP)
    await feed(group_msg(left_chat_member=MEMBER))
    assert deleted(session) == [joined.message.message_id]


async def test_missing_delete_right_is_reported_once(feed, app, session, moderator):
    await setup_group(feed, app)
    for _ in range(2):
        session.queue(DeleteMessage, TelegramBadRequest(method=None, message="Bad Request: message can't be deleted"))
        await feed(group_msg(text="spam.site"))
    reports = [c for c in session.calls(SendMessage) if c.chat_id == OWNER_ID and "не работает" in c.text]
    assert len(reports) == 1


async def test_strangers_cannot_press_admin_buttons_in_group(feed, app, session, moderator):
    await setup_group(feed, app)
    session.clear()
    await feed(press_in_group(Nav(to="settings"), STRANGER))
    assert session.requests == []


# ---------------------------------------------------------------------- подписка


def member_handler(subscribed: set[int], *, broken: bool = False):
    def handler(method: GetChatMember):
        if method.user_id == BOT_ID:
            return admin_member()
        if broken:
            raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
        user = User(id=method.user_id, is_bot=False, first_name="U")
        return ChatMemberMember(user=user) if method.user_id in subscribed else ChatMemberLeft(user=user)

    return handler


async def setup_subscription(feed, app, moderator):
    chat = await setup_group(feed, app)
    channel = await add_chat(feed, app, CHANNEL, chat_type="channel")
    await app.settings.set_list(app.repo, "sub_channels", [channel.id])
    moderator.forget()
    return chat, channel


async def test_unsubscribed_member_gets_notice(feed, app, session, moderator):
    chat, channel = await setup_subscription(feed, app, moderator)
    subscribed: set[int] = set()
    session.handlers[GetChatMember] = member_handler(subscribed)
    session.clear()

    first = group_msg(text="Привет всем")
    await feed(first)
    assert first.message.message_id in deleted(session)
    notices = group_sends(session)
    assert len(notices) == 1 and "подпишитесь" in notices[0].text and f"tg://user?id={STRANGER_ID}" in notices[0].text
    buttons = [b for row in notices[0].reply_markup.inline_keyboard for b in row]
    assert buttons[0].url == f"https://t.me/+invite{CHANNEL}"  # закрытый канал — ссылка-приглашение
    assert buttons[-1].callback_data == SubCheck(u=STRANGER_ID).pack()
    assert (await app.repo.get_chat(channel.id)).invite_link  # ссылка сохранена

    second = group_msg(text="Ну и?")
    await feed(second)
    assert second.message.message_id in deleted(session)
    assert len(group_sends(session)) == 1  # повторная подсказка — не сразу
    assert await app.repo.moderation_counts(0, chat.id) == (0, 2)

    await feed(press_in_group(SubCheck(u=STRANGER_ID), STRANGER))
    answer = session.calls(AnswerCallbackQuery)[-1]
    assert answer.show_alert and "не подписаны" in answer.text

    subscribed.add(STRANGER_ID)
    await feed(press_in_group(SubCheck(u=STRANGER_ID), STRANGER, message_id=4242))
    assert "Спасибо" in session.calls(AnswerCallbackQuery)[-1].text
    assert 4242 in deleted(session)  # подсказка убрана
    kept = group_msg(text="Теперь можно")
    await feed(kept)
    assert kept.message.message_id not in deleted(session)


async def test_other_member_pressing_does_not_remove_notice(feed, app, session, moderator):
    await setup_subscription(feed, app, moderator)
    session.handlers[GetChatMember] = member_handler({MEMBER.id})
    await feed(press_in_group(SubCheck(u=STRANGER_ID), MEMBER, message_id=5151))
    assert "Спасибо" in session.calls(AnswerCallbackQuery)[-1].text
    assert 5151 not in deleted(session)


async def test_subscription_modes(feed, app, session, moderator):
    chat, _channel = await setup_subscription(feed, app, moderator)
    session.handlers[GetChatMember] = member_handler(set())
    await app.repo.update_chat(chat.id, sub_mode="off")
    moderator.forget()
    await feed(group_msg(text="Без проверки"))
    assert deleted(session) == []

    other = await add_chat(feed, app, -1003000000888, chat_type="channel")
    await app.repo.update_chat(chat.id, sub_mode="own", sub_channels=[other.id])
    await app.settings.set_list(app.repo, "sub_channels", [])
    moderator.forget()
    own = group_msg(text="Свой канал")
    await feed(own)
    assert own.message.message_id in deleted(session)
    checked = {c.chat_id for c in session.calls(GetChatMember) if c.user_id == STRANGER_ID}
    assert checked == {-1003000000888}


async def test_unsubscribed_chat_admin_can_write(feed, app, session, moderator):
    await setup_subscription(feed, app, moderator)
    session.handlers[GetChatMember] = member_handler(set())
    session.handlers[GetChatAdministrators] = lambda method: [chat_admin(STRANGER)]
    await feed(group_msg(text="Я админ"))
    assert deleted(session) == []


async def test_unverifiable_channel_does_not_block_chat(feed, app, session, moderator):
    await setup_subscription(feed, app, moderator)
    session.handlers[GetChatMember] = member_handler(set(), broken=True)
    for _ in range(2):
        await feed(group_msg(text="Пишу"))
    assert deleted(session) == []
    reports = [c for c in session.calls(SendMessage) if c.chat_id == OWNER_ID and "проверить подписку" in c.text]
    assert len(reports) == 1


async def test_network_trouble_is_not_reported(feed, app, session, moderator):
    from aiogram.exceptions import TelegramNetworkError

    await setup_subscription(feed, app, moderator)

    def flaky(method: GetChatMember):
        if method.user_id == BOT_ID:
            return admin_member()
        raise TelegramNetworkError(method=method, message="timeout")

    session.handlers[GetChatMember] = flaky
    await feed(group_msg(text="Пишу"))
    assert deleted(session) == []
    assert not [c for c in session.calls(SendMessage) if c.chat_id == OWNER_ID and "проверить подписку" in c.text]


async def test_old_messages_are_removed_without_notice(feed, app, session, moderator):
    await setup_subscription(feed, app, moderator)
    session.handlers[GetChatMember] = member_handler(set())
    session.clear()
    old = group_msg(text="Написал, пока бот спал", date=datetime.now() - timedelta(minutes=10))
    await feed(old)
    assert deleted(session) == [old.message.message_id]
    assert group_sends(session) == []


async def test_group_traffic_does_not_grow_fsm_storage(feed, dp, app, moderator):
    await setup_group(feed, app)
    for n in range(30):
        await feed(group_msg(user=User(id=50_000 + n, is_bot=False, first_name="U"), text="привет"))
    assert len(dp.fsm.storage.storage) == 0
