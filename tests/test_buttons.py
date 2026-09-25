from __future__ import annotations

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

from bot.services.buttons import (
    ButtonParseError,
    buttons_from_markup,
    buttons_to_html,
    buttons_to_markup,
    normalize_url,
    parse_buttons,
    utf16_to_index,
)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_rows_and_columns():
    rows = parse_buttons("Сайт - https://a.ru | Канал - t.me/chan\nКупить - shop.ru/item?id=1")
    assert rows == [
        [{"text": "Сайт", "url": "https://a.ru"}, {"text": "Канал", "url": "https://t.me/chan"}],
        [{"text": "Купить", "url": "https://shop.ru/item?id=1"}],
    ]


def test_style_and_dashes_in_label():
    rows = parse_buttons("Скидка - 50% - https://x.ru - green\nВыход — tg://resolve?domain=x — красная")
    assert rows[0][0] == {"text": "Скидка - 50%", "url": "https://x.ru", "style": "success"}
    assert rows[1][0] == {"text": "Выход", "url": "tg://resolve?domain=x", "style": "danger"}


def test_copy_button_with_style():
    rows = parse_buttons("Промокод - copy:SALE 2026 - blue")
    assert rows == [[{"text": "Промокод", "copy_text": "SALE 2026", "style": "primary"}]]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@my_channel", "https://t.me/my_channel"),
        ("t.me/x", "https://t.me/x"),
        ("www.site.ru", "https://www.site.ru"),
        ("сайт.рф", "https://сайт.рф"),
        ("http://a.b/c", "http://a.b/c"),
        ("просто текст", None),
        ("ftp://x", None),
        ("@ab", None),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Без ссылки",
        "Кнопка - не ссылка",
        " - https://a.ru",
        "Код - copy:",
        "A - https://a.ru\n" + "|".join(["B - https://b.ru"] * 9),
    ],
)
def test_errors(text):
    with pytest.raises(ButtonParseError):
        parse_buttons(text)


def test_error_mentions_line_number():
    with pytest.raises(ButtonParseError, match="Строка 2"):
        parse_buttons("Ок - https://a.ru\nПлохая строка")


def test_custom_emoji_becomes_icon_with_utf16_offsets():
    # Перед кнопкой — эмодзи из двух UTF-16 единиц, чтобы смещения Python и Telegram разошлись
    text = "😀 Первая - https://a.ru\n🔥 Вторая - https://b.ru | 🎁 Третья - https://c.ru"
    second_line = text.index("🔥")
    third = text.index("🎁")
    entities = [
        MessageEntity(type="custom_emoji", offset=utf16_len(text[:second_line]), length=2, custom_emoji_id="111"),
        MessageEntity(type="custom_emoji", offset=utf16_len(text[:third]), length=2, custom_emoji_id="222"),
    ]
    rows = parse_buttons(text, entities)
    assert rows[0][0] == {"text": "😀 Первая", "url": "https://a.ru"}  # обычный эмодзи остаётся в тексте
    assert rows[1][0] == {
        "text": "Вторая",
        "url": "https://b.ru",
        "icon_custom_emoji_id": "111",
        "icon_emoji": "🔥",
    }
    assert rows[1][1]["icon_custom_emoji_id"] == "222"


def test_custom_emoji_only_label_keeps_text():
    text = "🔥 - https://a.ru"
    rows = parse_buttons(text, [MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="1")])
    assert rows == [[{"text": "🔥", "url": "https://a.ru"}]]


def test_utf16_to_index():
    text = "a😀b"
    assert utf16_to_index(text, 0) == 0
    assert utf16_to_index(text, 1) == 1
    assert utf16_to_index(text, 3) == 2


def test_markup_roundtrip_and_icons_toggle():
    rows = [
        [{"text": "A", "url": "https://a.ru", "style": "success", "icon_custom_emoji_id": "5"}],
        [{"text": "Код", "copy_text": "X1"}],
    ]
    markup = buttons_to_markup(rows)
    assert markup.inline_keyboard[0][0].icon_custom_emoji_id == "5"
    assert markup.inline_keyboard[0][0].style == "success"
    assert markup.inline_keyboard[1][0].copy_text.text == "X1"
    plain = buttons_to_markup(rows, with_icons=False)
    assert plain.inline_keyboard[0][0].icon_custom_emoji_id is None
    assert buttons_to_markup(None) is None
    assert buttons_from_markup(markup) == rows


def test_import_skips_callback_buttons():
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Лайк", callback_data="like"),
                InlineKeyboardButton(text="Сайт", url="https://s.ru"),
            ],
            [InlineKeyboardButton(text="Только колбэк", callback_data="x")],
        ]
    )
    assert buttons_from_markup(markup) == [[{"text": "Сайт", "url": "https://s.ru"}]]


def test_to_html_escapes_and_shows_icons():
    html_text = buttons_to_html(
        [
            [
                {
                    "text": "<b>",
                    "url": "https://a.ru?x=1&y=2",
                    "style": "danger",
                    "icon_custom_emoji_id": "9",
                    "icon_emoji": "🔥",
                }
            ]
        ]
    )
    assert html_text == '<tg-emoji emoji-id="9">🔥</tg-emoji> &lt;b&gt; - https://a.ru?x=1&amp;y=2 - red'
