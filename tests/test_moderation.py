"""Защита групп через настоящий диспетчер: антиспам и обязательная подписка."""

# Фикстуры диспетчера берутся из test_flows — pytest передаёт их в тесты по имени
# ruff: noqa: F811

from __future__ import annotations

import itertools
import time
from datetime import datetime, timedelta
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import (
    AnswerCallbackQuery,
    CreateChatInviteLink,
    DeleteMessage,
    DeleteMessages,
    EditMessageReplyMarkup,
    GetChat,
    GetChatAdministrators,
    GetChatMember,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, ChatMemberLeft, ChatMemberMember, Message, Update, User

from bot.services.moderation import LINK_LIFETIME, NOTICE_LIFETIME, SUB_NO_TTL, Moderator, invite_name
from bot.ui.callbacks import Nav, SubCheck
from tests.conftest import BOT_ID, OWNER_ID, STRANGER_ID, FakeClock, admin_member, chat_info
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


def clocked(app, clock: FakeClock) -> Moderator:
    """Модератор со своими часами — для проверок, где важно время (кэши, повтор подсказки)."""
    instance = Moderator(app, clock=clock)
    instance.notice_lifetime = 0
    app.moderator = instance
    return instance


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


def invites(session, chat_id: int = CHANNEL) -> list[CreateChatInviteLink]:
    return [call for call in session.calls(CreateChatInviteLink) if call.chat_id == chat_id]


def notice_links(notice: SendMessage) -> list[str]:
    return [b.url for row in notice.reply_markup.inline_keyboard for b in row if b.url]


def owner_warnings(session, needle: str) -> list[SendMessage]:
    return [c for c in session.calls(SendMessage) if c.chat_id == OWNER_ID and needle in c.text]


def notice_id_in_group(session, message_id: int) -> None:
    """Подсказка в группе получит заданный id — чтобы проверить, что её удалили."""

    def handler(method: SendMessage):
        if method.chat_id != GROUP:
            return session._default(method)
        return Message(
            message_id=message_id, date=datetime.now(), chat=Chat(id=GROUP, type="supergroup"), text=method.text
        )

    session.handlers[SendMessage] = handler


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


async def setup_subscription(feed, app, moderator, *, can_invite: bool = True, **channel: Any):
    """Группа и канал для подписки. По умолчанию у бота в канале есть право на ссылки-приглашения."""
    chat = await setup_group(feed, app)
    added = await add_chat(feed, app, CHANNEL, chat_type="channel")
    await app.repo.update_chat(added.id, can_invite=can_invite, **channel)
    await app.settings.set_list(app.repo, "sub_channels", [added.id])
    moderator.forget()
    return chat, await app.repo.get_chat(added.id)


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
    assert buttons[0].url.startswith(f"https://t.me/+invite{CHANNEL}_")  # личная ссылка-приглашение
    assert buttons[-1].text == "✅ Проверить подписку"
    assert buttons[-1].callback_data == SubCheck(u=STRANGER_ID).pack()
    (invite,) = invites(session)
    assert invite.member_limit == 1 and invite.name == f"Спамер · {STRANGER_ID}"
    expire = invite.expire_date.timestamp() if isinstance(invite.expire_date, datetime) else invite.expire_date
    assert abs(expire - (time.time() + LINK_LIFETIME)) < 60
    assert (await app.repo.get_chat(channel.id)).invite_link is None  # личные ссылки не хранятся

    second = group_msg(text="Ну и?")
    await feed(second)
    assert second.message.message_id in deleted(session)
    assert len(group_sends(session)) == 1  # пока подсказка висит — удаляется молча
    assert len(invites(session)) == 1
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


async def test_every_notice_gets_new_personal_links(feed, app, session, clock):
    moderator = clocked(app, clock)
    await setup_subscription(feed, app, moderator, username="our_channel")
    session.handlers[GetChatMember] = member_handler(set())
    session.clear()

    await feed(group_msg(text="Первый"), group_msg(user=MEMBER, text="Второй"))
    assert [call.name for call in invites(session)] == [f"Спамер · {STRANGER_ID}", f"Участник · {MEMBER.id}"]
    links = [notice_links(notice) for notice in group_sends(session)]
    assert len(links) == 2 and links[0] != links[1]
    assert all(link[0].startswith("https://t.me/+invite") for link in links)  # и у публичного канала — личные

    clock.advance(NOTICE_LIFETIME + 1)  # прошлая подсказка исчезла — следующая с новой ссылкой
    await feed(group_msg(text="Снова"))
    assert len(invites(session)) == 3
    again = notice_links(group_sends(session)[-1])
    assert len(group_sends(session)) == 3 and again[0] not in {links[0][0], links[1][0]}


async def test_public_channel_without_invite_right_gets_plain_link(feed, app, session, moderator):
    await setup_subscription(feed, app, moderator, can_invite=False, username="our_channel")
    session.handlers[GetChatMember] = member_handler(set())
    session.clear()
    await feed(group_msg(text="Привет"), group_msg(user=MEMBER, text="И я"))
    assert invites(session) == []  # права нет — Telegram не просим
    assert [notice_links(notice) for notice in group_sends(session)] == [["https://t.me/our_channel"]] * 2
    (warning,) = owner_warnings(session, "личные ссылки")  # раз в сутки, а не на каждого
    assert "обычная ссылка" in warning.text


async def test_private_channel_without_invite_right_has_no_link(feed, app, session, moderator):
    await setup_subscription(feed, app, moderator, can_invite=False)
    session.handlers[GetChatMember] = member_handler(set())
    session.clear()
    await feed(group_msg(text="Привет"))
    (notice,) = group_sends(session)
    assert notice_links(notice) == []
    assert notice.reply_markup.inline_keyboard[-1][0].callback_data == SubCheck(u=STRANGER_ID).pack()
    (warning,) = owner_warnings(session, "личные ссылки")
    assert "нет кнопки" in warning.text


async def test_flood_limit_falls_back_to_reserve_link(feed, app, session, moderator):
    await setup_subscription(feed, app, moderator, invite_link="https://t.me/+reserve")
    session.handlers[GetChatMember] = member_handler(set())

    def flood(method: CreateChatInviteLink):
        raise TelegramRetryAfter(method=method, message="Too Many Requests: retry after 30", retry_after=30)

    session.handlers[CreateChatInviteLink] = flood
    session.clear()
    await feed(group_msg(text="Привет"), group_msg(user=MEMBER, text="И я"))
    assert len(invites(session)) == 1  # на время паузы Telegram больше не просим
    assert [notice_links(notice) for notice in group_sends(session)] == [["https://t.me/+reserve"]] * 2
    assert owner_warnings(session, "личные ссылки") == []  # это ненадолго — админов не беспокоим


async def test_refused_invite_link_pauses_until_rights_change(feed, app, session, moderator):
    await setup_subscription(feed, app, moderator, username="our_channel")

    def refuse(method: CreateChatInviteLink):
        raise TelegramBadRequest(method=method, message="Bad Request: not enough rights to manage chat invite link")

    session.handlers[GetChatMember] = member_handler(set())
    session.handlers[CreateChatInviteLink] = refuse
    session.clear()
    await feed(group_msg(text="Привет"), group_msg(user=MEMBER, text="И я"))
    assert len(invites(session)) == 1
    assert [notice_links(notice) for notice in group_sends(session)] == [["https://t.me/our_channel"]] * 2
    (warning,) = owner_warnings(session, "личные ссылки")
    assert "not enough rights to manage chat invite link" in warning.text  # видно, что ответил Telegram

    del session.handlers[CreateChatInviteLink]
    moderator.forget(CHANNEL)  # права бота поменялись (my_chat_member) — пауза снимается
    await feed(group_msg(user=User(id=3001, is_bot=False, first_name="Новый"), text="Здравствуйте"))
    assert notice_links(group_sends(session)[-1])[0].startswith(f"https://t.me/+invite{CHANNEL}_")


async def test_notice_disappears_when_member_subscribes_and_writes(feed, app, session, clock):
    moderator = clocked(app, clock)
    moderator.notice_lifetime = 60  # сама за время теста не исчезнет
    await setup_subscription(feed, app, moderator)
    subscribed: set[int] = set()
    session.handlers[GetChatMember] = member_handler(subscribed)
    notice_id_in_group(session, 9001)
    await feed(group_msg(text="Привет"))
    assert 9001 not in deleted(session)

    subscribed.add(STRANGER_ID)
    clock.advance(SUB_NO_TTL + 1)  # «не подписан» бот помнит недолго
    kept = group_msg(text="Подписался, не нажимая кнопку")
    await feed(kept)
    assert kept.message.message_id not in deleted(session)
    assert 9001 in deleted(session)  # подсказка исчезла


async def test_live_notices_are_removed_on_shutdown(feed, app, session, moderator):
    moderator.notice_lifetime = NOTICE_LIFETIME
    await setup_subscription(feed, app, moderator)
    session.handlers[GetChatMember] = member_handler(set())
    notice_id_in_group(session, 9002)
    await feed(group_msg(text="Привет"))
    assert 9002 not in deleted(session)
    await moderator.close()  # после перезапуска удалять её было бы некому
    assert 9002 in deleted(session)


def test_invite_name_fits_telegram_limit():
    assert invite_name("Иван Петров", 6434205268) == "Иван Петров · 6434205268"
    assert invite_name("Имя\nс  переносом", 7) == "Имя с переносом · 7"
    assert invite_name("   ", 42) == "42"
    long = invite_name("😀" * 40 + " Очень длинное имя", 10**15)
    assert long.endswith(f" · {10**15}") and long.startswith("😀")
    assert len(long.encode("utf-16-le")) // 2 <= 32


async def test_only_addressee_can_use_check_button(feed, app, session, moderator):
    await setup_subscription(feed, app, moderator)
    session.handlers[GetChatMember] = member_handler({MEMBER.id})  # нажавший подписан, адресат — нет
    session.clear()
    await feed(press_in_group(SubCheck(u=STRANGER_ID), MEMBER, message_id=5151))
    answer = session.calls(AnswerCallbackQuery)[-1]
    assert answer.show_alert and "для участника, к которому обращается подсказка" in answer.text
    assert [c for c in session.calls(GetChatMember) if c.user_id == MEMBER.id] == []  # его подписку не проверяли
    assert 5151 not in deleted(session)  # подсказка на месте

    blocked = group_msg(text="А я всё равно пишу")  # адресат по-прежнему не может писать
    await feed(blocked)
    assert blocked.message.message_id in deleted(session)


async def test_admin_sees_addressee_status(feed, app, session, moderator):
    await setup_subscription(feed, app, moderator)
    subscribed: set[int] = set()
    session.handlers[GetChatMember] = member_handler(subscribed)
    owner = User(id=OWNER_ID, is_bot=False, first_name="Владелец")
    await feed(press_in_group(SubCheck(u=STRANGER_ID), owner, message_id=6161))
    answer = session.calls(AnswerCallbackQuery)[-1]
    assert answer.show_alert and "ещё не подписан" in answer.text and "писать не сможет" in answer.text
    assert 6161 not in deleted(session)

    subscribed.add(STRANGER_ID)
    await feed(press_in_group(SubCheck(u=STRANGER_ID), owner, message_id=6161))
    assert "уже подписан" in session.calls(AnswerCallbackQuery)[-1].text
    assert 6161 in deleted(session)  # подсказка больше не нужна


async def test_failed_check_refreshes_personal_links(feed, app, session, clock):
    moderator = clocked(app, clock)
    await setup_subscription(feed, app, moderator)
    session.handlers[GetChatMember] = member_handler(set())
    session.clear()
    await feed(press_in_group(SubCheck(u=STRANGER_ID), STRANGER, message_id=7171))
    (edit,) = session.calls(EditMessageReplyMarkup)
    assert edit.message_id == 7171
    links = [b.url for row in edit.reply_markup.inline_keyboard for b in row if b.url]
    assert links and links[0].startswith(f"https://t.me/+invite{CHANNEL}_")  # новая личная ссылка
    assert "Ссылки в подсказке обновлены" in session.calls(AnswerCallbackQuery)[-1].text

    await feed(press_in_group(SubCheck(u=STRANGER_ID), STRANGER, message_id=7171))
    assert len(session.calls(EditMessageReplyMarkup)) == 1  # не чаще раза в 30 секунд
    clock.advance(31)
    await feed(press_in_group(SubCheck(u=STRANGER_ID), STRANGER, message_id=7171))
    assert len(session.calls(EditMessageReplyMarkup)) == 2


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
