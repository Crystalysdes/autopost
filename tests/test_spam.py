"""Распознавание спама: какие сообщения считаются спамом, а какие — нет."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram.types import Message

from bot.services.spam import (
    DEFAULT_STOP_WORDS,
    SpamRules,
    build_allowlist,
    compile_stop_words,
    detect,
    has_user_content,
    is_join_leave,
)

CHAT = {"id": -100500, "type": "supergroup", "title": "Группа", "username": "our_group"}
USER = {"id": 42, "is_bot": False, "first_name": "Иван"}
CHANNEL = {"id": -100777, "type": "channel", "title": "Чужой канал"}
WORDS = compile_stop_words(DEFAULT_STOP_WORDS)
ALLOW = build_allowlist(["good.ru", "@friends_channel"], extra_names=["our_group", "autopost_test_bot"])


def ent(kind: str, text: str, part: str, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "offset": text.index(part), "length": len(part), **extra}


def msg(text: str | None = None, *, entities: list[dict[str, Any]] | None = None, **fields: Any) -> Message:
    data: dict[str, Any] = {"message_id": 1, "date": 1, "chat": CHAT, "from": USER}
    if text is not None:
        data["text"] = text
    if entities:
        data["entities"] = entities
    data.update(fields)
    return Message.model_validate(data)


def reason(message: Message, rules: SpamRules | None = None, allow=ALLOW) -> str | None:
    return detect(message, rules or SpamRules(), stop_words=WORDS, allow=allow).reason


# ---------------------------------------------------------------------------- ссылки


@pytest.mark.parametrize(
    "text",
    [
        "Заходи на example.com",
        "https://spam.site/offer",
        "Канал: t . me/cryptoclub",
        "t,me/joinchat/abc",
        "tg://resolve?domain=spam",
        "www.casino.org",
        "Наш сайт — магазин.рф",
        "telegra.ph/Zarabotok-01-01",
    ],
)
def test_links_in_text(text):
    assert reason(msg(text)) == "links"


def test_links_in_entities_and_buttons():
    text = "Подробнее тут"
    hidden = msg(text, entities=[ent("text_link", text, "тут", url="https://spam.site")])
    assert reason(hidden) == "links"
    mail = "Пишите spam@mail.ru"
    assert reason(msg(mail, entities=[ent("email", mail, "spam@mail.ru")])) == "links"
    button = msg("Смотри", reply_markup={"inline_keyboard": [[{"text": "Открыть", "url": "https://x.io"}]]})
    assert reason(button) == "links"


@pytest.mark.parametrize(
    "text",
    [
        "т.е. завтра в 12.30",
        "и т.д. и т.п.",
        "1.5 литра",
        "скинь photo.jpg и file.pdf",
        "Привет.Как дела",
        "ok, me too",
        "мы заработали на этом",
        "@vasya, глянь",
        "Всем привет! Когда встреча?",
    ],
)
def test_normal_messages_are_not_spam(text):
    verdict = detect(msg(text), SpamRules(), stop_words=WORDS, allow=ALLOW)
    assert verdict.reason is None


def test_allowlist():
    assert reason(msg("Смотри good.ru и shop.good.ru")) is None  # домен вместе с поддоменами
    assert reason(msg("Наш канал t.me/friends_channel")) is None
    assert reason(msg("Чат группы: t.me/our_group")) is None  # собственный юзернейм чата
    assert reason(msg("А тут t.me/other_channel")) == "links"
    assert reason(msg("good.ru и bad.ru")) == "links"  # разрешены должны быть все ссылки


# ------------------------------------------------------------------ боты и каналы


def test_bots():
    assert reason(msg("Жми", via_bot={"id": 5, "is_bot": True, "first_name": "B", "username": "inline_bot"})) == "bots"
    text = "Запускай @Earn_Money_Bot"
    assert reason(msg(text, entities=[ent("mention", text, "@Earn_Money_Bot")])) == "bots"
    command = "/start@other_bot"
    assert reason(msg(command, entities=[ent("bot_command", command, command)])) == "bots"
    own = "/start@autopost_test_bot"
    assert reason(msg(own, entities=[ent("bot_command", own, own)])) is None
    callback = msg("Игра", reply_markup={"inline_keyboard": [[{"text": "Играть", "callback_data": "x"}]]})
    assert reason(callback) == "bots"


def test_mentions_are_left_for_the_moderator():
    text = "Подписывайся на @crypto_news и пиши @vasya"
    verdict = detect(
        msg(text, entities=[ent("mention", text, "@crypto_news"), ent("mention", text, "@vasya")]),
        SpamRules(),
        stop_words=WORDS,
        allow=ALLOW,
    )
    assert verdict.reason is None and verdict.mentions == ["crypto_news", "vasya"]
    allowed = detect(msg("Заходите в @friends_channel"), SpamRules(), stop_words=WORDS, allow=ALLOW)
    assert allowed.mentions == []


# ----------------------------------------------------------- пересылки и каналы


def test_forwards():
    origin = {"type": "channel", "date": 1, "chat": CHANNEL, "message_id": 5}
    assert reason(msg("Смотрите", forward_origin=origin)) == "forwards"
    hidden = {"type": "hidden_user", "date": 1, "sender_user_name": "Кто-то"}
    assert reason(msg("Смотрите", forward_origin=hidden)) == "forwards"
    assert reason(msg(story={"chat": CHANNEL, "id": 3})) == "forwards"
    assert reason(msg("Круто же", external_reply={"origin": origin})) == "forwards"
    linked = msg("Пост канала", forward_origin=origin, is_automatic_forward=True, sender_chat=CHANNEL)
    assert reason(linked) is None  # пост привязанного канала в группе обсуждения


def test_channels():
    assert reason(msg("Привет от канала", sender_chat=CHANNEL)) == "channels"
    assert reason(msg("Анонимный админ", sender_chat=CHAT)) is None


# --------------------------------------------------------- контакты, слова, имя


def test_contacts():
    assert reason(msg(contact={"phone_number": "+79990001122", "first_name": "Менеджер"})) == "contacts"
    assert reason(msg(location={"latitude": 55.7, "longitude": 37.6})) == "contacts"
    text = "Звони +7 999 000-11-22"
    assert reason(msg(text, entities=[ent("phone_number", text, "+7 999 000-11-22")])) == "contacts"


@pytest.mark.parametrize(
    "text",
    [
        "Заработок от 3000 руб в день",
        "зapaбoтoк без вложeний",  # латинские буквы вперемешку
        "за​работок",  # невидимый символ внутри
        "Пишите в лс",
        "от 500$ в день",
        "Контент 18+",
        "Удалённая работа, подработка",
    ],
)
def test_stop_words(text):
    assert reason(msg(text)) == "words"


def test_custom_stop_words():
    words = compile_stop_words(["промокод", "скидк"])
    rules = SpamRules()
    assert detect(msg("Дарю промокод"), rules, stop_words=words, allow=ALLOW).reason == "words"
    assert detect(msg("Скидки!"), rules, stop_words=words, allow=ALLOW).reason == "words"
    assert detect(msg("Заработок"), rules, stop_words=words, allow=ALLOW).reason is None


def test_ad_in_sender_name():
    for name in ("Заработок в тг", "Анна @anna_nails", "Магазин shop.ru"):
        user = {"id": 7, "is_bot": False, "first_name": name}
        assert reason(msg("Привет всем", **{"from": user})) == "names", name
    assert reason(msg("Привет всем")) is None


def test_link_in_poll():
    poll = {
        "id": "1",
        "question": "Где заказать?",
        "options": [
            {"persistent_id": "a", "text": "На spam.site", "voter_count": 0},
            {"persistent_id": "b", "text": "Нигде", "voter_count": 0},
        ],
        "total_voter_count": 0,
        "is_closed": False,
        "is_anonymous": True,
        "type": "regular",
        "allows_multiple_answers": False,
        "allows_revoting": False,
        "members_only": False,
    }
    assert reason(msg(poll=poll)) == "links"


# ---------------------------------------------------------------------- правила


def test_rules_can_be_turned_off():
    text = "Смотри spam.site"
    assert reason(msg(text), SpamRules(on=False)) is None
    assert reason(msg(text), SpamRules(links=False)) is None
    assert reason(msg("Заработок в тг"), SpamRules(words=False)) is None
    origin = {"type": "channel", "date": 1, "chat": CHANNEL, "message_id": 5}
    assert reason(msg("Смотрите", forward_origin=origin), SpamRules(forwards=False)) is None


def test_rules_roundtrip():
    rules = SpamRules.of({"links": False, "on": True, "unknown": True})
    assert rules.links is False and rules.words is True
    assert SpamRules.of(rules.to_json()) == rules
    assert SpamRules.of(None) == SpamRules()


def test_service_messages():
    joined = msg(new_chat_members=[USER])
    assert is_join_leave(joined) and not has_user_content(joined)
    pinned = msg(pinned_message={"message_id": 1, "date": 0, "chat": CHAT})
    assert not is_join_leave(pinned) and not has_user_content(pinned)
    assert has_user_content(msg("текст"))
