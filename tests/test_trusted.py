"""Скрытые админы: защита их не трогает; добавление кнопкой внизу, пересылкой и текстом."""

# Фикстуры диспетчера берутся из test_flows — pytest передаёт их в тесты по имени
# ruff: noqa: F811

from __future__ import annotations

import json
from datetime import datetime

import pytest
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import AnswerCallbackQuery, GetChatMember
from aiogram.types import (
    Chat,
    MessageOriginChannel,
    MessageOriginHiddenUser,
    MessageOriginUser,
    SharedUser,
    Update,
    User,
    UsersShared,
)

from bot.app import AppSettings
from bot.services import trusted
from bot.services.moderation import Moderator
from bot.states import Input
from bot.ui import keyboards
from bot.ui.callbacks import Guard, Nav
from tests.conftest import OWNER_ID
from tests.test_flows import (  # noqa: F401
    add_chat,
    button_data,
    clock,
    dp,
    feed,
    last_markup,
    owner_texts,
    press,
    private_message,
    state_of,
)
from tests.test_moderation import CHANNEL, GROUP, deleted, group_msg, group_sends, member_handler

MANAGER = User(id=7001, is_bot=False, first_name="Менеджер", username="Sales_Manager")
OTHER_GROUP = -1003000000002
CHANNEL_BOT = User(id=136817688, is_bot=True, first_name="Channel")  # от его имени приходят посты «от канала»
OUR_CHANNEL = Chat(id=-1001000000555, type="channel", title="Наш канал", username="our_channel")


@pytest.fixture
async def moderator(app):
    instance = Moderator(app)
    instance.notice_lifetime = 0
    app.moderator = instance
    yield instance
    await instance.close()


def users_shared(*users: User) -> Update:
    shared = UsersShared(
        request_id=keyboards.REQUEST_TRUSTED,
        users=[SharedUser(user_id=u.id, first_name=u.first_name, username=u.username) for u in users],
    )
    return private_message(users_shared=shared)


def forwarded(origin) -> Update:
    return private_message("пересланное сообщение", forward_origin=origin)


async def trust_everywhere(app, moderator, *entries) -> None:
    await app.settings.set_list(app.repo, "trusted", list(entries))
    moderator.forget()


# ------------------------------------------------------------------------ защита


async def test_hidden_admin_from_common_list_is_not_checked(feed, app, session, moderator):
    await add_chat(feed, app, GROUP)
    await trust_everywhere(app, moderator, trusted.from_user(MANAGER))
    session.clear()
    link = group_msg(user=MANAGER, text="Скидки тут: shop.example.com")
    origin = MessageOriginChannel(
        date=datetime.now(), chat=Chat(id=-100777, type="channel", title="Реклама"), message_id=5
    )
    forward = group_msg(user=MANAGER, text="Пересланный пост", forward_origin=origin)
    spam = group_msg(text="shop.example.com")
    await feed(link, forward, spam)
    assert deleted(session) == [spam.message.message_id]  # удалён только чужой спам


async def test_chat_list_works_only_in_that_chat(feed, app, session, moderator):
    first = await add_chat(feed, app, GROUP)
    await add_chat(feed, app, OTHER_GROUP)
    await app.repo.update_chat(first.id, trusted=[trusted.from_user(MANAGER)])
    moderator.forget()
    session.clear()
    here = group_msg(GROUP, user=MANAGER, text="spam.site")
    there = group_msg(OTHER_GROUP, user=MANAGER, text="spam.site")
    await feed(here, there)
    assert deleted(session, GROUP) == []
    assert deleted(session, OTHER_GROUP) == [there.message.message_id]


async def test_username_and_channel_entries(feed, app, session, moderator):
    await add_chat(feed, app, GROUP)
    await trust_everywhere(app, moderator, trusted.make(username="sales_manager"), trusted.from_chat(OUR_CHANNEL))
    session.clear()
    by_username = group_msg(user=MANAGER, text="spam.site")  # @Sales_Manager — регистр не важен
    as_channel = group_msg(user=CHANNEL_BOT, sender_chat=OUR_CHANNEL, text="Реклама: spam.site")
    stranger = Chat(id=-1001000000556, type="channel", title="Чужой канал")
    foreign = group_msg(user=CHANNEL_BOT, sender_chat=stranger, text="Привет")
    await feed(by_username, as_channel, foreign)
    assert deleted(session) == [foreign.message.message_id]


async def test_unsubscribed_hidden_admin_can_write(feed, app, session, moderator):
    await add_chat(feed, app, GROUP)
    channel = await add_chat(feed, app, CHANNEL, chat_type="channel")
    await app.settings.set_list(app.repo, "sub_channels", [channel.id])
    await trust_everywhere(app, moderator, trusted.from_user(MANAGER))
    session.handlers[GetChatMember] = member_handler(set())
    session.clear()
    await feed(group_msg(user=MANAGER, text="Пишу без подписки"))
    assert deleted(session) == [] and group_sends(session) == []


# ------------------------------------------------------------------------ добавление


async def test_add_by_text_and_remove(feed, dp, bot, app, session, moderator):
    await feed(press(Nav(to="settings")))
    assert Guard(a="trust").pack() in button_data(last_markup(session))
    assert "Скрытые админы во всех группах: 0" in owner_texts(session)[-1]
    await feed(press(Guard(a="trust")), press(Guard(a="tadd")))
    assert await state_of(dp, bot) == Input.trusted.state

    await feed(private_message("не то!"))
    assert "Не похоже на @username или ID" in owner_texts(session)[-1]
    assert await state_of(dp, bot) == Input.trusted.state  # ввод продолжается

    await feed(private_message("@Ivan_Petrov, 123456789"))
    assert await state_of(dp, bot) is None
    assert app.settings.trusted == [trusted.make(username="ivan_petrov"), trusted.make(ident=123456789)]
    assert "✅ Добавлены" in owner_texts(session)[-1]
    assert json.loads((await app.repo.load_settings())["trusted"]) == app.settings.trusted

    await feed(press(Guard(a="tdel", v=trusted.code(trusted.make(ident=123456789)))))
    assert app.settings.trusted == [trusted.make(username="ivan_petrov")]
    assert session.calls(AnswerCallbackQuery)[-1].text == "Убран: ID 123456789"


async def test_add_to_chat_by_forward(feed, dp, bot, app, session, moderator):
    group = await add_chat(feed, app, GROUP)
    await feed(press(Guard(a="chat", id=group.id)))
    assert Guard(a="trust", id=group.id).pack() in button_data(last_markup(session))
    await feed(press(Guard(a="trust", id=group.id)), press(Guard(a="tadd", id=group.id)))

    await feed(forwarded(MessageOriginHiddenUser(date=datetime.now(), sender_user_name="Аноним")))
    assert "скрыл пересылку" in owner_texts(session)[-1]
    assert await state_of(dp, bot) == Input.trusted.state

    await feed(forwarded(MessageOriginUser(date=datetime.now(), sender_user=MANAGER)))
    assert (await app.repo.get_chat(group.id)).trusted == [trusted.from_user(MANAGER)]
    assert app.settings.trusted == []  # общий список не тронут

    await feed(
        press(Guard(a="tadd", id=group.id)),
        forwarded(MessageOriginChannel(date=datetime.now(), chat=OUR_CHANNEL, message_id=1)),
    )
    assert (await app.repo.get_chat(group.id)).trusted[-1] == {
        "id": OUR_CHANNEL.id,
        "username": "our_channel",
        "name": "Наш канал",
    }
    await feed(press(Guard(a="chat", id=group.id)))
    assert "🕶 Скрытые админы: 2 в этом чате · 0 во всех группах" in owner_texts(session)[-1]


async def test_picker_during_input_adds_to_that_chat(feed, dp, bot, app, session, moderator):
    group = await add_chat(feed, app, GROUP)
    await feed(press(Guard(a="tadd", id=group.id)), users_shared(MANAGER))
    assert (await app.repo.get_chat(group.id)).trusted == [
        trusted.make(ident=MANAGER.id, username="sales_manager", name="Менеджер")
    ]
    assert await state_of(dp, bot) is None


async def test_picker_asks_where_to_add(feed, app, session, moderator):
    group = await add_chat(feed, app, GROUP)
    await feed(users_shared(MANAGER))
    assert "Куда добавить" in owner_texts(session)[-1]
    data = button_data(last_markup(session))
    assert Guard(a="tput", id=0).pack() in data and Guard(a="tput", id=group.id).pack() in data

    await feed(press(Guard(a="tput", id=group.id)))
    assert (await app.repo.get_chat(group.id)).trusted[0]["id"] == MANAGER.id
    assert "✅ Добавлены" in owner_texts(session)[-1]

    await feed(press(Guard(a="tput", id=0)))  # этот выбор уже использован
    answer = session.calls(AnswerCallbackQuery)[-1]
    assert answer.show_alert and "устарел" in answer.text
    assert app.settings.trusted == []


async def test_picker_is_not_taken_as_post(feed, dp, bot, app, session, moderator):
    key = StorageKey(bot_id=bot.id, chat_id=OWNER_ID, user_id=OWNER_ID)
    await dp.fsm.storage.set_state(key, Input.posts)
    await dp.fsm.storage.set_data(key, {"camp_id": 1})
    await feed(users_shared(MANAGER))
    assert "Куда добавить" in owner_texts(session)[-1]


async def test_adding_same_person_twice(feed, app, session, moderator):
    await feed(press(Guard(a="tadd")), users_shared(MANAGER))
    await feed(press(Guard(a="tadd")), private_message("@sales_manager"))
    assert app.settings.trusted == [trusted.from_user(MANAGER)]
    assert "уже в списке" in owner_texts(session)[-1]


# ------------------------------------------------------------------------ прочее


def test_bottom_keyboard_has_user_picker():
    button = keyboards.add_chat_reply().keyboard[1][0]
    assert button.text == keyboards.ADD_TRUSTED_TEXT
    assert button.request_users.request_id == keyboards.REQUEST_TRUSTED
    assert button.request_users.user_is_bot is False and button.request_users.request_username is True


async def test_settings_load_and_clean(app):
    await app.repo.save_setting("trusted", json.dumps([{"id": 5, "name": "A"}, {"bad": 1}, "x"]))
    settings = AppSettings("Europe/Moscow")
    await settings.load(app.repo)
    assert settings.trusted == [{"id": 5, "username": None, "name": "A"}]

    await app.repo.save_setting("trusted", "не JSON")
    settings = AppSettings("Europe/Moscow")
    await settings.load(app.repo)
    assert settings.trusted == []


def test_parse_merge_remove():
    make = trusted.make
    entries, wrong = trusted.parse_text("@Ivan_P, t.me/our_channel\n123 -1001234 плохо")
    assert entries == [make(username="ivan_p"), make(username="our_channel"), make(ident=123), make(ident=-1001234)]
    assert wrong == ["плохо"]

    # был только @username — теперь известен и id: запись уточняется, а не дублируется
    merged, added = trusted.merge([make(username="ivan_p")], [make(ident=5, username="Ivan_P", name="Иван")])
    assert merged == [make(ident=5, username="ivan_p", name="Иван")] and len(added) == 1
    assert trusted.merge(merged, [make(ident=5)])[1] == []

    full = [make(ident=i) for i in range(1, trusted.MAX_TRUSTED + 1)]
    assert trusted.merge(full, [make(ident=10**6)])[1] == []  # список полон

    rest, removed = trusted.remove(merged, trusted.code(merged[0]))
    assert rest == [] and removed == merged[0]
    assert trusted.label(make(ident=7)) == "ID 7"
    assert trusted.label(merged[0]) == "Иван · @ivan_p"
