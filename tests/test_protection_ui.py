"""Настройка защиты групп из бота: экраны, переключатели, списки."""

from __future__ import annotations

# Фикстуры диспетчера берутся из test_flows — pytest передаёт их в тесты по имени
# ruff: noqa: F811
from datetime import datetime

import pytest
from aiogram.methods import AnswerCallbackQuery, DeleteMessage
from aiogram.types import Chat, Message, Update, User

from bot.services.moderation import Moderator
from bot.states import Input
from bot.ui.callbacks import Guard, Nav, SubCheck
from tests.conftest import STRANGER_ID
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

GROUP = -1004000000001
STRANGER = User(id=STRANGER_ID, is_bot=False, first_name="Спамер")


@pytest.fixture
async def moderator(app):
    instance = Moderator(app)
    instance.notice_lifetime = 0
    app.moderator = instance
    yield instance
    await instance.close()


def group_deletes(session, chat_id: int = GROUP) -> list[DeleteMessage]:
    """Удаления в группе (в личке бот ещё убирает свои подсказки ввода — их не считаем)."""
    return [call for call in session.calls(DeleteMessage) if call.chat_id == chat_id]


def group_text(text: str, chat_id: int = GROUP) -> Update:
    message = Message(
        message_id=int(datetime.now().timestamp() * 1000) % 10**9,
        date=datetime.now(),
        chat=Chat(id=chat_id, type="supergroup", title="Группа"),
        from_user=STRANGER,
        text=text,
    )
    return Update(update_id=message.message_id, message=message)


async def test_chat_screen_has_protection_only_for_groups(feed, app, session, moderator):
    group = await add_chat(feed, app, GROUP)
    channel = await add_chat(feed, app, -1004000000999, chat_type="channel")
    await feed(press(Nav(to="chat", id=group.id)))
    assert Guard(a="chat", id=group.id).pack() in button_data(last_markup(session))
    await feed(press(Nav(to="chat", id=channel.id)))
    assert not any(data.startswith("m:") for data in button_data(last_markup(session)))


async def test_toggles_change_what_is_deleted(feed, app, session, moderator):
    group = await add_chat(feed, app, GROUP)
    await feed(press(Guard(a="chat", id=group.id)))
    assert "Защита «" in owner_texts(session)[-1]

    await feed(press(Guard(a="r_links", id=group.id, v=0)))
    await feed(group_text("Мой сайт example.com"))
    assert group_deletes(session) == []  # ссылки разрешены

    await feed(press(Guard(a="spam", id=group.id, v=0)))
    await feed(group_text("Заработок без вложений"))
    assert group_deletes(session) == []  # антиспам выключен
    assert (await app.repo.get_chat(group.id)).spam_filter == {**_all_on(), "links": False, "on": False}

    await feed(press(Guard(a="spam", id=group.id, v=1)))
    await feed(group_text("Заработок без вложений"))
    assert len(group_deletes(session)) == 1


def _all_on() -> dict[str, bool]:
    return {
        name: True for name in ("on", "links", "bots", "forwards", "channels", "words", "contacts", "names", "service")
    }


async def test_subscription_modes_and_channels(feed, app, session, moderator):
    group = await add_chat(feed, app, GROUP)
    first = await add_chat(feed, app, -1004000000901, chat_type="channel")
    second = await add_chat(feed, app, -1004000000902, chat_type="channel")

    await feed(press(Guard(a="sub")), press(Guard(a="ch", id=0)), press(Guard(a="con", id=0, v=first.id)))
    assert app.settings.sub_channels == [first.id]
    assert "Общие каналы для подписки" in owner_texts(session)[-1]

    await feed(press(Guard(a="sm", id=group.id, v=1)))  # свои каналы — для начала копия общих
    chat = await app.repo.get_chat(group.id)
    assert chat.sub_mode == "own" and chat.sub_channels == [first.id]
    await feed(press(Guard(a="con", id=group.id, v=second.id)), press(Guard(a="coff", id=group.id, v=first.id)))
    assert (await app.repo.get_chat(group.id)).sub_channels == [second.id]
    assert app.settings.sub_channels == [first.id]  # общие не тронуты

    await feed(press(Guard(a="sm", id=group.id, v=2)))
    assert (await app.repo.get_chat(group.id)).sub_mode == "off"
    await feed(press(Guard(a="sub_reset")))
    assert (await app.repo.get_chat(group.id)).sub_mode is None


async def test_unavailable_channel_cannot_be_checked(feed, app, session, moderator):
    await add_chat(feed, app, GROUP)
    await feed(press(Guard(a="con", id=0, v=9999)))
    assert session.calls(AnswerCallbackQuery)[-1].show_alert
    assert app.settings.sub_channels == []


async def test_stop_words_input(feed, dp, bot, app, session, moderator):
    await add_chat(feed, app, GROUP)
    await feed(press(Guard(a="spam_set")), press(Guard(a="words")))
    assert await state_of(dp, bot) == Input.spam_words.state
    await feed(private_message("промокод\nскидк, распродаж"))
    assert app.settings.spam_words == ["промокод", "скидк", "распродаж"]
    assert await state_of(dp, bot) is None
    await feed(group_text("Большие скидки!"))
    assert len(group_deletes(session)) == 1
    await feed(group_text("Заработок без вложений"))  # стандартных слов больше нет в списке
    assert len(group_deletes(session)) == 1
    await feed(press(Guard(a="words_reset")))
    assert app.settings.spam_words is None
    assert (await app.repo.load_settings()).get("spam_words") is None


async def test_allowlist_input(feed, dp, bot, app, session, moderator):
    await add_chat(feed, app, GROUP)
    await feed(press(Guard(a="allow")), private_message("не домен вовсе"))
    assert "Не похоже" in owner_texts(session)[-1]
    assert await state_of(dp, bot) == Input.spam_allow.state
    await feed(private_message("MySite.ru\n@our_channel\nt.me/partner"))
    assert app.settings.spam_allow == ["mysite.ru", "@our_channel", "t.me/partner"]
    await feed(group_text("Смотрите mysite.ru и t.me/partner"))
    assert group_deletes(session) == []
    await feed(press(Guard(a="allow_clear")))
    await feed(group_text("Смотрите mysite.ru"))
    assert len(group_deletes(session)) == 1


async def test_spam_everywhere(feed, app, session, moderator):
    first = await add_chat(feed, app, GROUP)
    second = await add_chat(feed, app, -1004000000002)
    await feed(press(Guard(a="spam_all", v=0)))
    for chat in (first, second):
        assert (await app.repo.get_chat(chat.id)).spam_filter["on"] is False
    await feed(group_text("spam.site"))
    assert group_deletes(session) == []
    await feed(press(Guard(a="spam_all", v=1)))
    await feed(group_text("spam.site"))
    assert len(group_deletes(session)) == 1


async def test_log_screen_and_deleted_chat(feed, app, session, moderator):
    group = await add_chat(feed, app, GROUP)
    await feed(group_text("spam.site"), press(Guard(a="log", id=group.id)))
    assert "spam.site" in owner_texts(session)[-1]
    await app.repo.delete_chat(group.id)
    await feed(press(Guard(a="chat", id=group.id)))
    assert session.calls(AnswerCallbackQuery)[-1].text == "Чат уже удалён"


def test_callback_data_fits_limit():
    big = 2**31 - 1
    for sample in (Guard(a="r_forwards", id=big, v=big), Guard(a="spam_all", v=1), SubCheck(u=10**12)):
        assert len(sample.pack().encode()) <= 64, sample
