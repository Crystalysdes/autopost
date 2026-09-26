"""Скрытые админы: люди и каналы, чьи сообщения защита чатов не трогает.

Для бота это те же админы чата: антиспам и обязательная подписка их пропускают, хотя в Telegram они
не админы и значка у них нет. Доступа к панели бота они не получают.

Запись — словарь {"id": int | None, "username": str | None, "name": str}:
- по id совпадение надёжное (id выдаёт кнопка выбора человека, пересылка или ввод числом);
- по @username — пока его не сменят;
- отрицательный id — канал или группа: так пропускаются сообщения «от имени канала».
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Iterable, Sequence
from typing import Any

from bot.services.rights import enum_text

MAX_TRUSTED = 100  # записей в одном списке
NAME_LIMIT = 64

Entry = dict[str, Any]

_USERNAME = re.compile(r"^[a-z][a-z0-9_]{3,31}$")
_ID = re.compile(r"^-?[1-9]\d{0,19}$")
_LINK = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]+)/?$", re.IGNORECASE)
_SPLIT = re.compile(r"[\s,;]+")


def make(*, ident: int | None = None, username: str | None = None, name: str = "") -> Entry:
    nick = (username or "").lstrip("@").lower() or None
    return {"id": ident, "username": nick, "name": " ".join((name or "").split())[:NAME_LIMIT]}


def clean(raw: Any) -> Entry | None:
    """Запись из базы. Повреждённая (нет ни id, ни юзернейма) — None."""
    if not isinstance(raw, dict):
        return None
    ident = raw.get("id")
    if isinstance(ident, bool) or not isinstance(ident, int) or ident == 0:
        ident = None
    nick = raw.get("username")
    nick = nick.lstrip("@").lower() if isinstance(nick, str) else ""
    if not _USERNAME.match(nick):
        nick = None
    if ident is None and nick is None:
        return None
    name = raw.get("name")
    return make(ident=ident, username=nick, name=name if isinstance(name, str) else "")


def clean_list(raw: Any) -> list[Entry]:
    if not isinstance(raw, list):
        return []
    return [entry for item in raw if (entry := clean(item)) is not None]


def key(entry: Entry) -> str:
    return f"id:{entry['id']}" if entry.get("id") is not None else f"@{entry['username']}"


def code(entry: Entry) -> int:
    """Короткий код записи для кнопки «❌» (callback не вмещает длинные ключи)."""
    return zlib.crc32(key(entry).encode()) & 0x7FFFFFFF


def label(entry: Entry) -> str:
    """«Имя · @ник» (текст не экранирован). Без имени и ника — «ID 123»."""
    parts = [part for part in (entry.get("name"), f"@{entry['username']}" if entry.get("username") else "") if part]
    return " · ".join(parts) if parts else f"ID {entry['id']}"


def is_channel(entry: Entry) -> bool:
    return entry.get("id") is not None and entry["id"] < 0


# ---------------------------------------------------------------------------- откуда берутся


def from_user(user: Any) -> Entry:
    return make(ident=user.id, username=user.username, name=user.full_name)


def from_chat(chat: Any) -> Entry:
    return make(ident=chat.id, username=chat.username, name=chat.title or "")


def from_shared(shared: Any) -> Entry:
    """Человек из кнопки выбора Telegram (users_shared)."""
    name = " ".join(part for part in (shared.first_name, shared.last_name) if part)
    return make(ident=shared.user_id, username=shared.username, name=name)


def from_forward(origin: Any) -> Entry | None:
    """Автор пересланного сообщения. None — человек скрыл пересылку, id не узнать."""
    kind = enum_text(getattr(origin, "type", ""))
    if kind == "user":
        return from_user(origin.sender_user)
    if kind == "channel":
        return from_chat(origin.chat)
    if kind == "chat":
        return from_chat(origin.sender_chat)
    return None


def parse_text(text: str) -> tuple[list[Entry], list[str]]:
    """@username, t.me/username, username или числовой ID — через пробел, запятую или с новой строки.
    Возвращает записи и куски, которые не удалось понять."""
    entries: list[Entry] = []
    wrong: list[str] = []
    for token in _SPLIT.split(text.strip()):
        if not token:
            continue
        if _ID.match(token):
            entries.append(make(ident=int(token)))
            continue
        link = _LINK.match(token)
        nick = (link.group(1) if link else token.lstrip("@")).lower()
        if _USERNAME.match(nick):
            entries.append(make(username=nick))
        else:
            wrong.append(token)
    return entries, wrong


# ---------------------------------------------------------------------------- списки


def merge(existing: Sequence[Entry], new: Iterable[Entry]) -> tuple[list[Entry], list[Entry]]:
    """Добавляет новые записи без повторов. Возвращает (итоговый список, что добавлено).
    Если человек уже был по @username, а теперь известен и его id, запись уточняется."""
    result = [dict(entry) for entry in existing]
    added: list[Entry] = []
    for entry in new:
        index = _find(result, entry)
        if index is not None:
            current = result[index]
            if current.get("id") is None and entry.get("id") is not None:
                result[index] = {**entry, "name": entry["name"] or current["name"]}
                added.append(result[index])
            elif entry["name"]:
                current["name"] = entry["name"]
            continue
        if len(result) >= MAX_TRUSTED:
            break
        result.append(dict(entry))
        added.append(entry)
    return result, added


def _find(entries: Sequence[Entry], entry: Entry) -> int | None:
    for index, current in enumerate(entries):
        if entry.get("id") is not None and current.get("id") == entry["id"]:
            return index
        if entry.get("username") and current.get("username") == entry["username"]:
            return index
    return None


def remove(entries: Sequence[Entry], entry_code: int) -> tuple[list[Entry], Entry | None]:
    for index, entry in enumerate(entries):
        if code(entry) == entry_code:
            return [*entries[:index], *entries[index + 1 :]], entry
    return list(entries), None


def match_sets(entries: Iterable[Entry]) -> tuple[frozenset[int], frozenset[str]]:
    """Множества id и юзернеймов для быстрой проверки сообщений."""
    ids: set[int] = set()
    names: set[str] = set()
    for entry in entries:
        if entry.get("id") is not None:
            ids.add(entry["id"])
        if entry.get("username"):
            names.add(entry["username"])
    return frozenset(ids), frozenset(names)
