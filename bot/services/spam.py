"""Распознавание спама в сообщениях групп. Только чистые функции — без сети и базы.

Какие признаки учитывать, решают правила (переключатели защиты чата). @упоминания, которые могут
оказаться ботом или каналом, возвращаются отдельно: это проверяет модератор (bot/services/moderation.py)
через Telegram, а люди («@вася, глянь») спамом не считаются.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, fields
from typing import Any
from urllib.parse import urlsplit

from aiogram.types import Message, MessageEntity

# Порядок важен: причина удаления — первое сработавшее правило
RULES = ("forwards", "channels", "bots", "links", "contacts", "words", "length", "names", "service")
RULE_TITLES = {
    "links": "🔗 Ссылки",
    "bots": "🤖 Боты и каналы",
    "forwards": "↪️ Пересылки",
    "channels": "📢 От имени каналов",
    "words": "🚫 Стоп-слова",
    "length": "📏 Длинные сообщения",
    "contacts": "📇 Контакты и гео",
    "names": "👤 Реклама в имени",
    "service": "🚪 Вход и выход",
}
REASON_TITLES = {**RULE_TITLES, "sub": "🔒 Нет подписки"}

DEFAULT_STOP_WORDS = (
    "заработок",
    "заработк",
    "заработать",
    "заработаешь",
    "заработай",
    "зарабатывай",
    "пассивн доход",
    "без вложений",
    "в лс",
    "в личку",
    "в личные сообщения",
    "работа на дому",
    "удаленн работ",
    "удаленк",
    "подработк",
    "крипт",
    "инвестиц",
    "казино",
    "ставки на спорт",
    "букмекер",
    "1xbet",
    "1win",
    "интим",
    "эскорт",
    "18+",
    "onlyfans",
    "онлифанс",
    "наркот",
    "обнал",
    "руб в день",
    "рублей в день",
    "₽ в день",
    "$ в день",
    "долларов в день",
)

# Зоны, на которые заканчиваются рекламные домены. Нет «файловых» (jpg, pdf, zip, mov, py, js…),
# чтобы «photo.jpg» и «file.pdf» не считались ссылками.
_TLDS = (
    "com|ru|net|org|info|biz|io|me|co|xyz|top|site|online|shop|store|pro|club|link|live|app|dev|cc|tk|ml|ga|cf|"
    "gq|su|ua|by|kz|uz|am|az|ge|kg|tj|de|uk|us|eu|in|ly|gg|tv|fm|to|ws|ai|bio|click|fun|space|website|cloud|"
    "digital|email|work|world|today|life|blog|news|vip|win|bet|casino|games|game|sale|promo|money|cash|finance|"
    "plus|best|icu|one|buzz|cyou|sbs|cfd|quest|rocks|pw|network|lol|market|agency|media|group|team|tech|host|"
    "press|page|art|mobi|asia|cn|tr|pl|cz|fr|es|nl|ch|at|se|fi|lv|lt|ee|il|ae|br|ca|au"
)
_LINK_PATTERNS = (
    re.compile(r"https?\s*:\s*/\s*/"),
    re.compile(r"(?<![\w.])www\."),
    # t.me/…, telegram.me/…, в том числе «t . me/» и «t,me/»
    re.compile(r"(?<![a-z0-9_])(?:t|telegram)\s*[.,·•。․]\s*(?:me|dog)\s*/"),
    re.compile(r"(?<![a-z0-9_])telegra\.ph\b"),
    re.compile(r"(?<![a-z0-9_])tg\s*:\s*/\s*/"),
    re.compile(rf"(?<![\w.@-])(?:[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?\.)+(?:{_TLDS})(?![\w-])(?:/\S*)?"),
    re.compile(r"(?<![\w.@-])(?:[а-я0-9](?:[а-я0-9-]{0,61}[а-я0-9])?\.)+рф(?![\w-])(?:/\S*)?"),
)
_TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.dog"}
_USERNAME_IN_TEXT = re.compile(r"(?<![\w@])@([a-z][a-z0-9_]{3,31})(?![\w])")
_AT_IN_NAME = re.compile(r"@\s*[a-z0-9_]{4,}")

_INVISIBLE = dict.fromkeys(
    map(ord, "­͏؜ᅟᅠ឴឵᠎​‌‍‎‏‪‫‬‭‮⁠⁡⁢⁣⁤⁦⁧⁨⁩ㅤ﻿ﾠ"),
    None,
)
# Латинские и цифровые «двойники» кириллицы: спамеры пишут «зapaбoтoк» вперемешку
_LOOKALIKES = str.maketrans("aeopcxykmthb30", "аеорсхукмтнвзо")


@dataclass(frozen=True)
class SpamRules:
    """Защита чата от спама. В базе хранится как словарь; None — всё включено."""

    on: bool = True
    links: bool = True
    bots: bool = True
    forwards: bool = True
    channels: bool = True
    words: bool = True
    length: bool = True  # длиннее лимита из настроек антиспама (если он задан)
    contacts: bool = True
    names: bool = True
    service: bool = True

    @classmethod
    def of(cls, data: dict[str, Any] | None) -> SpamRules:
        data = data or {}
        return cls(**{f.name: bool(data.get(f.name, f.default)) for f in fields(cls)})

    def to_json(self) -> dict[str, bool]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def enabled(self, rule: str) -> bool:
        return self.on and bool(getattr(self, rule))


@dataclass(frozen=True)
class Allowlist:
    """Разрешённые ссылки: домены (вместе с поддоменами) и @имена (в том числе в ссылках t.me/имя)."""

    hosts: frozenset[str] = frozenset()
    names: frozenset[str] = frozenset()

    def allows_url(self, raw: str) -> bool:
        host, first = _url_parts(raw)
        if not host:
            return False
        if host in _TELEGRAM_HOSTS:
            return bool(first) and first in self.names
        return any(host == allowed or host.endswith("." + allowed) for allowed in self.hosts)


@dataclass
class Verdict:
    reason: str | None = None  # правило, по которому сообщение — спам
    mentions: list[str] = field(default_factory=list)  # @ники: бот или канал — решает модератор


def build_allowlist(entries: Iterable[str], extra_names: Iterable[str | None] = ()) -> Allowlist:
    """entries — строки из настроек: «site.ru», «https://t.me/mychannel», «@mychannel»."""
    hosts: set[str] = set()
    names = {name.lower().lstrip("@") for name in extra_names if name}
    for entry in entries:
        value = entry.strip().lower()
        if not value:
            continue
        if value.startswith("@"):
            names.add(value[1:])
            continue
        host, first = _url_parts(value)
        if host in _TELEGRAM_HOSTS:
            if first:
                names.add(first)
        elif host:
            hosts.add(host)
    return Allowlist(hosts=frozenset(hosts), names=frozenset(names))


def normalize(text: str) -> str:
    """Для поиска: без невидимых символов, в нижнем регистре, «ё» → «е», пробелы схлопнуты."""
    text = unicodedata.normalize("NFKC", text).translate(_INVISIBLE).casefold().replace("ё", "е")
    return " ".join(text.split())


def compile_stop_words(words: Sequence[str]) -> list[tuple[re.Pattern[str], re.Pattern[str]]]:
    """Каждое слово стоп-фразы совпадает с началом слова в тексте («заработ» → «заработок»); короткие
    слова (до 3 букв) — только целиком, чтобы «в лс» не ловило «вот лс». Вторая регулярка — для текста,
    где латинские двойники заменены кириллицей."""
    compiled = []
    for word in words:
        phrase = normalize(word)
        if not phrase:
            continue
        parts = []
        for part in phrase.split(" "):
            tail = r"\w*" if len(part) > 3 and part[-1].isalnum() else r"(?!\w)"
            parts.append(re.escape(part) + tail)
        # «Начало слова» — только если фраза начинается с буквы или цифры: «500$ в день» тоже спам
        head = r"(?<!\w)" if phrase[0].isalnum() else ""
        plain = head + r"[\s\W]*".join(parts)
        looks = head + r"[\s\W]*".join(p.translate(_LOOKALIKES) for p in parts)
        compiled.append((re.compile(plain), re.compile(looks)))
    return compiled


def has_stop_word(text: str, compiled: Sequence[tuple[re.Pattern[str], re.Pattern[str]]]) -> bool:
    if not text:
        return False
    plain = normalize(text)
    looks = plain.translate(_LOOKALIKES)
    return any(p.search(plain) or q.search(looks) for p, q in compiled)


def find_links(text: str) -> list[str]:
    """Ссылки в тексте, которые Telegram мог не распознать: без схемы, с пробелами, «t . me/…»."""
    found = []
    plain = normalize(text)
    for pattern in _LINK_PATTERNS:
        for match in pattern.finditer(plain):
            start = match.start()
            found.append(plain[start : _token_end(plain, match.end())])
    return found


def is_join_leave(message: Message) -> bool:
    return bool(message.new_chat_members or message.left_chat_member)


def has_user_content(message: Message) -> bool:
    """Сообщение от человека (текст, медиа, пересылка…), а не служебное событие чата."""
    return any(
        getattr(message, name, None)
        for name in (
            "text",
            "caption",
            "photo",
            "video",
            "animation",
            "document",
            "audio",
            "voice",
            "video_note",
            "sticker",
            "story",
            "contact",
            "location",
            "venue",
            "poll",
            "dice",
            "game",
            "invoice",
            "paid_media",
            "checklist",
            "giveaway",
            "giveaway_winners",
            "forward_origin",
        )
    )


def message_texts(message: Message) -> list[str]:
    """Весь текст, который видят участники: сообщение или подпись, опрос, чек-лист."""
    texts = [message.text or message.caption or ""]
    if message.poll:
        texts.append(message.poll.question)
        texts += [option.text for option in message.poll.options]
    if message.checklist:
        texts.append(message.checklist.title)
        texts += [task.text for task in message.checklist.tasks]
    return [text for text in texts if text]


def snippet(message: Message, limit: int = 100) -> str:
    text = " ".join((message.text or message.caption or "").split())
    if not text and message.poll:
        text = message.poll.question
    return text[:limit] if text else f"[{message.content_type}]"


def detect(
    message: Message,
    rules: SpamRules,
    *,
    stop_words: Sequence[tuple[re.Pattern[str], re.Pattern[str]]],
    allow: Allowlist,
    max_len: int = 0,
) -> Verdict:
    """Спам ли это. Служебные сообщения (вход/выход) и исключения (админы и т. п.) решает модератор.
    max_len — сколько символов текста разрешено (0 — без ограничения)."""
    if not rules.on:
        return Verdict()
    text = message.text or message.caption or ""
    entities: list[MessageEntity] = list(message.entities or message.caption_entities or [])
    texts = message_texts(message)

    if rules.forwards and (
        (message.forward_origin and not message.is_automatic_forward)
        or message.story
        or message.external_reply
        or message.giveaway
        or message.giveaway_winners
        or message.paid_media
    ):
        return Verdict("forwards")
    if (
        rules.channels
        and message.sender_chat
        and message.sender_chat.id != message.chat.id
        and not message.is_automatic_forward
    ):
        return Verdict("channels")

    mentions: list[str] = []
    if rules.bots:
        if message.via_bot or message.game or message.invoice or _has_callback_buttons(message):
            return Verdict("bots")
        for entity in entities:
            if entity.type == "bot_command":
                command = entity.extract_from(text)
                target = command.partition("@")[2].lower()
                if target and target not in allow.names:
                    return Verdict("bots")
        for name in _mentions(text, entities, texts):
            if name in allow.names:
                continue
            if name.endswith("bot"):
                return Verdict("bots")
            if name not in mentions:
                mentions.append(name)

    if rules.links:
        links = [url for url in _button_urls(message)]
        for entity in entities:
            if entity.type == "text_link" and entity.url:
                links.append(entity.url)
            elif entity.type in ("url", "email"):
                links.append(entity.extract_from(text))
        for chunk in texts:
            links += find_links(chunk)
        if any(not allow.allows_url(link) for link in links):
            return Verdict("links", mentions)

    if rules.contacts and (
        message.contact
        or message.location
        or message.venue
        or any(entity.type == "phone_number" for entity in entities)
    ):
        return Verdict("contacts", mentions)

    if rules.words and any(has_stop_word(chunk, stop_words) for chunk in texts):
        return Verdict("words", mentions)

    if rules.length and max_len > 0 and sum(len(chunk) for chunk in texts) > max_len:
        return Verdict("length", mentions)

    if rules.names and message.from_user and not message.sender_chat:
        name = message.from_user.full_name
        plain = normalize(name)
        if find_links(name) or _AT_IN_NAME.search(plain) or has_stop_word(name, stop_words):
            return Verdict("names", mentions)

    return Verdict(None, mentions)


# ---------------------------------------------------------------------------- детали


def _token_end(text: str, end: int) -> int:
    while end < len(text) and not text[end].isspace():
        end += 1
    return end


def _url_parts(raw: str) -> tuple[str, str]:
    """(хост, первая часть пути) — «https://T.me/Chan/5» -> ("t.me", "chan")."""
    value = normalize(raw).replace(" ", "")
    if "@" in value.split("/")[0] and "://" not in value:  # e-mail: user@mail.ru
        value = value.split("@", 1)[1]
    if "://" not in value:
        value = "//" + value
    try:
        parts = urlsplit(value)
    except ValueError:
        return "", ""
    host = (parts.hostname or "").removeprefix("www.")
    first = parts.path.strip("/").split("/")[0] if parts.path else ""
    return host, first.lstrip("@")


def _mentions(text: str, entities: Sequence[MessageEntity], texts: Sequence[str]) -> list[str]:
    names = []
    for entity in entities:
        if entity.type == "mention":
            names.append(entity.extract_from(text).lstrip("@").lower())
    for chunk in texts:
        names += [match.group(1) for match in _USERNAME_IN_TEXT.finditer(normalize(chunk))]
    return list(dict.fromkeys(names))


def _buttons(message: Message) -> list[Any]:
    markup = message.reply_markup
    if markup is None:
        return []
    return [button for row in markup.inline_keyboard for button in row]


def _button_urls(message: Message) -> list[str]:
    return [button.url for button in _buttons(message) if button.url]


def _has_callback_buttons(message: Message) -> bool:
    """Кнопки без ссылок у сообщения участника бывают только через ботов."""
    return any(not button.url for button in _buttons(message))
