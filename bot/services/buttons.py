"""Кнопки под постом: разбор текстового синтаксиса, сборка клавиатуры, импорт из пересланного поста.

Синтаксис (одна строка — один ряд, «|» разделяет кнопки в ряду):

    Текст - https://ссылка
    Кнопка 1 - https://a.ru | Кнопка 2 - t.me/channel
    Купить - https://shop.ru - green
    Промокод - copy:SALE2026

Премиум-эмодзи в самом начале текста кнопки становится её иконкой (icon_custom_emoji_id).
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from typing import Any

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

STYLE_ALIASES = {
    "green": "success",
    "зеленая": "success",
    "зелёная": "success",
    "зеленый": "success",
    "зелёный": "success",
    "success": "success",
    "red": "danger",
    "красная": "danger",
    "красный": "danger",
    "danger": "danger",
    "blue": "primary",
    "синяя": "primary",
    "синий": "primary",
    "primary": "primary",
}
STYLE_TITLES = {"success": "green", "danger": "red", "primary": "blue"}

MAX_PER_ROW = 8
MAX_BUTTONS = 100
MAX_TEXT = 64
MAX_COPY_TEXT = 256

_SEP_RE = re.compile(r"\s+[-—–]\s+")
_COPY_SEP_RE = re.compile(r"\s+[-—–]\s+copy:", re.IGNORECASE)
_TRAILING_STYLE_RE = re.compile(r"\s+[-—–]\s+(\S+)\s*$")
_USERNAME_RE = re.compile(r"^@([A-Za-z0-9_]{4,32})$")
_DOMAIN_RE = re.compile(r"^(?:[\w-]+\.)+[A-Za-zА-Яа-яЁё]{2,}(?::\d+)?(?:[/?#]\S*)?$")

Rows = list[list[dict[str, Any]]]


class ButtonParseError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        self.line = line
        super().__init__(f"Строка {line}: {message}" if line else message)


def utf16_to_index(text: str, utf16_offset: int) -> int:
    """Смещение в UTF-16 (как у Telegram) -> индекс символа в строке Python."""
    return len(text.encode("utf-16-le")[: utf16_offset * 2].decode("utf-16-le", errors="ignore"))


def _custom_emoji_positions(text: str, entities: Sequence[MessageEntity]) -> dict[int, tuple[int, str]]:
    """Индекс начала премиум-эмодзи -> (индекс конца, custom_emoji_id)."""
    result: dict[int, tuple[int, str]] = {}
    for entity in entities:
        if entity.type != "custom_emoji" or not entity.custom_emoji_id:
            continue
        start = utf16_to_index(text, entity.offset)
        end = utf16_to_index(text, entity.offset + entity.length)
        result[start] = (end, entity.custom_emoji_id)
    return result


def normalize_url(raw: str) -> str | None:
    value = raw.strip()
    if not value or any(ch.isspace() for ch in value):
        return None
    lowered = value.lower()
    if lowered.startswith(("http://", "https://", "tg://")):
        return value
    username = _USERNAME_RE.match(value)
    if username:
        return f"https://t.me/{username.group(1)}"
    if lowered.startswith(("t.me/", "telegram.me/", "www.")):
        return "https://" + value
    if _DOMAIN_RE.match(value):
        return "https://" + value
    return None


def _parse_segment(segment: str, segment_start: int, emoji_at: dict[int, tuple[int, str]], line: int) -> dict[str, Any]:
    body = segment.strip()
    body_start = segment_start + (len(segment) - len(segment.lstrip()))
    style: str | None = None
    target: dict[str, Any]

    copy_match = _COPY_SEP_RE.search(body)
    if copy_match:
        label = body[: copy_match.start()]
        rest = body[copy_match.end() :]
        style_match = _TRAILING_STYLE_RE.search(rest)
        if style_match and style_match.group(1).lower() in STYLE_ALIASES:
            style = STYLE_ALIASES[style_match.group(1).lower()]
            rest = rest[: style_match.start()]
        copy_text = rest.strip()
        if not copy_text:
            raise ButtonParseError(line, "после copy: нужен текст, который будет копироваться")
        if len(copy_text) > MAX_COPY_TEXT:
            raise ButtonParseError(line, f"текст для копирования длиннее {MAX_COPY_TEXT} символов")
        target = {"copy_text": copy_text}
    else:
        separators = list(_SEP_RE.finditer(body))
        if not separators:
            raise ButtonParseError(line, f"не нашёл ссылку в «{body}». Формат: Текст - https://ссылка")
        tail = body[separators[-1].end() :].strip()
        if len(separators) >= 2 and tail.lower() in STYLE_ALIASES:
            style = STYLE_ALIASES[tail.lower()]
            raw_url = body[separators[-2].end() : separators[-1].start()].strip()
            label = body[: separators[-2].start()]
        else:
            raw_url = tail
            label = body[: separators[-1].start()]
        url = normalize_url(raw_url)
        if url is None:
            raise ButtonParseError(line, f"непонятная ссылка «{raw_url}»")
        target = {"url": url}

    text = label.strip()
    button: dict[str, Any] = {}
    hit = emoji_at.get(body_start)
    if hit is not None:
        end_index, emoji_id = hit
        emoji_len = end_index - body_start
        rest_label = label[emoji_len:].strip()
        if rest_label:
            button["icon_custom_emoji_id"] = emoji_id
            button["icon_emoji"] = label[:emoji_len]
            text = rest_label
    if not text:
        raise ButtonParseError(line, "у кнопки нет текста")
    if len(text) > MAX_TEXT:
        raise ButtonParseError(line, f"текст кнопки длиннее {MAX_TEXT} символов")
    button = {"text": text, **target, **button}
    if style:
        button["style"] = style
    return button


def parse_buttons(text: str, entities: Sequence[MessageEntity] | None = None) -> Rows:
    emoji_at = _custom_emoji_positions(text, entities or [])
    rows: Rows = []
    total = 0
    position = 0
    for line_no, line in enumerate(text.split("\n"), start=1):
        line_start = position
        position += len(line) + 1
        if not line.strip():
            continue
        row = [
            _parse_segment(match.group(0), line_start + match.start(), emoji_at, line_no)
            for match in re.finditer(r"[^|]+", line)
            if match.group(0).strip()
        ]
        if not row:
            continue
        if len(row) > MAX_PER_ROW:
            raise ButtonParseError(line_no, f"в одном ряду может быть не больше {MAX_PER_ROW} кнопок")
        rows.append(row)
        total += len(row)
    if not rows:
        raise ButtonParseError(0, "Не нашёл ни одной кнопки. Формат: Текст - https://ссылка")
    if total > MAX_BUTTONS:
        raise ButtonParseError(0, f"Слишком много кнопок (максимум {MAX_BUTTONS})")
    return rows


def has_icons(rows: Rows | None) -> bool:
    return any(b.get("icon_custom_emoji_id") for row in rows or [] for b in row)


def count_buttons(rows: Rows | None) -> int:
    return sum(len(row) for row in rows or [])


def buttons_to_markup(rows: Rows | None, *, with_icons: bool = True) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    keyboard: list[list[InlineKeyboardButton]] = []
    for row in rows:
        built: list[InlineKeyboardButton] = []
        for button in row:
            kwargs: dict[str, Any] = {"text": button["text"]}
            if button.get("url"):
                kwargs["url"] = button["url"]
            elif button.get("copy_text"):
                kwargs["copy_text"] = CopyTextButton(text=button["copy_text"])
            else:
                continue
            if button.get("style"):
                kwargs["style"] = button["style"]
            if with_icons and button.get("icon_custom_emoji_id"):
                kwargs["icon_custom_emoji_id"] = button["icon_custom_emoji_id"]
            built.append(InlineKeyboardButton(**kwargs))
        if built:
            keyboard.append(built)
    return InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None


def buttons_from_markup(markup: InlineKeyboardMarkup | None) -> Rows | None:
    """URL-кнопки и кнопки копирования из пересланного поста (остальные типы пропускаются)."""
    if markup is None or not getattr(markup, "inline_keyboard", None):
        return None
    rows: Rows = []
    for row in markup.inline_keyboard:
        parsed: list[dict[str, Any]] = []
        for button in row:
            item: dict[str, Any] = {"text": button.text}
            if button.url:
                item["url"] = button.url
            elif button.copy_text is not None:
                item["copy_text"] = button.copy_text.text
            else:
                continue
            if button.style:
                item["style"] = button.style
            if button.icon_custom_emoji_id:
                item["icon_custom_emoji_id"] = button.icon_custom_emoji_id
            parsed.append(item)
        if parsed:
            rows.append(parsed)
    return rows or None


def buttons_to_html(rows: Rows | None) -> str:
    """Кнопки в том же синтаксисе, что и ввод — чтобы можно было скопировать и поправить."""
    lines: list[str] = []
    for row in rows or []:
        parts: list[str] = []
        for button in row:
            text = html.escape(button["text"])
            if button.get("icon_custom_emoji_id"):
                emoji = html.escape(button.get("icon_emoji") or "⭐")
                text = f'<tg-emoji emoji-id="{html.escape(button["icon_custom_emoji_id"])}">{emoji}</tg-emoji> {text}'
            target = (
                html.escape(button["url"]) if button.get("url") else "copy:" + html.escape(button.get("copy_text", ""))
            )
            part = f"{text} - {target}"
            if button.get("style"):
                part += f" - {STYLE_TITLES.get(button['style'], button['style'])}"
            parts.append(part)
        lines.append(" | ".join(parts))
    return "\n".join(lines)
