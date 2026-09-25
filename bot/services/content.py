"""Приём постов: превращаем сообщение (или альбом) админа в сохраняемый пост.

Пост хранится «как отправлен»: текст/подпись вместе с entities (форматирование и премиум-эмодзи),
спойлеры, подпись над медиа, настройки превью ссылок и file_id. Текст нигде не обрезается
и не strip-ится — иначе съедут смещения entities (они считаются в UTF-16).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aiogram.types import LinkPreviewOptions, Message, MessageEntity

from bot.services.buttons import Rows, buttons_from_markup

CAPTION_LIMIT = 1024
ALBUM_LIMIT = 10
ALBUM_TYPES = {"photo", "video", "animation", "document", "audio", "live_photo"}
SPOILER_TYPES = {"photo", "video", "animation", "live_photo"}
CAPTION_TYPES = {"photo", "video", "animation", "document", "audio", "voice", "live_photo"}

KIND_TITLES = {
    "text": "💬 Текст",
    "photo": "🖼 Фото",
    "video": "🎬 Видео",
    "animation": "🎞 GIF",
    "document": "📄 Файл",
    "audio": "🎵 Аудио",
    "voice": "🎤 Голосовое",
    "video_note": "⭕ Кружок",
    "sticker": "🌟 Стикер",
    "live_photo": "✨ Live-фото",
    "album": "🗂 Альбом",
}


class ContentError(ValueError):
    """Сообщение нельзя сохранить как пост; текст показывается пользователю."""


@dataclass
class Captured:
    kind: str
    payload: dict[str, Any]
    forward_from: dict[str, Any] | None
    buttons: Rows | None


def dump_entities(entities: Sequence[MessageEntity] | None) -> list[dict[str, Any]] | None:
    if not entities:
        return None
    return [e.model_dump(mode="json", exclude_none=True) for e in entities]


def load_entities(data: Sequence[dict[str, Any]] | None) -> list[MessageEntity] | None:
    if not data:
        return None
    return [MessageEntity.model_validate(item) for item in data]


def load_link_preview(data: dict[str, Any] | None) -> LinkPreviewOptions | None:
    return LinkPreviewOptions.model_validate(data) if data else None


def _reject_unsupported(message: Message) -> None:
    unsupported = [
        (message.story, "истории"),
        (message.poll, "опросы"),
        (message.checklist, "чек-листы"),
        (message.paid_media, "платные медиа"),
        (message.rich_message, "rich-сообщения"),
        (message.dice, "кубики"),
        (message.game, "игры"),
        (message.venue or message.location, "геопозиции"),
        (message.contact, "контакты"),
        (message.invoice, "счета"),
    ]
    for value, title in unsupported:
        if value:
            raise ContentError(f"Такой тип сообщений не поддерживается ({title}). Пришлите текст или медиа.")


def _media_item(message: Message) -> dict[str, Any]:
    item: dict[str, Any]
    if message.live_photo:
        photo = message.photo or message.live_photo.photo
        if not photo:
            raise ContentError("Не получилось прочитать live-фото. Попробуйте отправить его ещё раз.")
        item = {
            "type": "live_photo",
            "file_id": message.live_photo.file_id,
            "photo_file_id": photo[-1].file_id,
        }
    elif message.photo:
        item = {"type": "photo", "file_id": message.photo[-1].file_id}
    elif message.video:
        item = {"type": "video", "file_id": message.video.file_id}
    elif message.animation:
        # У GIF Telegram заполняет и поле document — поэтому animation проверяется раньше.
        item = {"type": "animation", "file_id": message.animation.file_id}
    elif message.document:
        item = {"type": "document", "file_id": message.document.file_id}
    elif message.audio:
        item = {"type": "audio", "file_id": message.audio.file_id}
    elif message.voice:
        item = {"type": "voice", "file_id": message.voice.file_id}
    elif message.video_note:
        item = {"type": "video_note", "file_id": message.video_note.file_id}
    elif message.sticker:
        item = {"type": "sticker", "file_id": message.sticker.file_id}
    else:
        raise ContentError("Не понял сообщение. Пришлите текст, фото, видео, GIF, файл, аудио или альбом.")

    if message.caption:
        if len(message.caption) > CAPTION_LIMIT:
            raise ContentError(
                f"Подпись слишком длинная: {len(message.caption)} символов. Боты могут отправлять "
                f"подписи до {CAPTION_LIMIT} символов — сократите её или отправьте текст отдельным постом."
            )
        item["caption"] = message.caption
        entities = dump_entities(message.caption_entities)
        if entities:
            item["caption_entities"] = entities
    if message.has_media_spoiler:
        item["has_spoiler"] = True
    if message.show_caption_above_media:
        item["show_caption_above_media"] = True
    return item


def _forward_source(messages: Sequence[Message]) -> dict[str, Any] | None:
    first = messages[0]
    if first.forward_origin is None:
        return None
    return {"chat_id": first.chat.id, "message_ids": [m.message_id for m in messages]}


def capture(message: Message, album: Sequence[Message] | None = None) -> Captured:
    if album and len(album) > 1:
        return _capture_album(sorted(album, key=lambda m: m.message_id))

    _reject_unsupported(message)
    payload: dict[str, Any]
    if message.text is not None:
        kind = "text"
        payload = {"text": message.text}
        entities = dump_entities(message.entities)
        if entities:
            payload["entities"] = entities
        if message.link_preview_options is not None:
            preview = message.link_preview_options.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            if preview:
                payload["link_preview"] = preview
    else:
        item = _media_item(message)
        kind = item["type"]
        payload = {"items": [item]}
    return Captured(
        kind=kind,
        payload=payload,
        forward_from=_forward_source([message]),
        buttons=buttons_from_markup(message.reply_markup),
    )


def _capture_album(messages: Sequence[Message]) -> Captured:
    if len(messages) > ALBUM_LIMIT:
        raise ContentError(f"В альбоме может быть не больше {ALBUM_LIMIT} файлов.")
    items: list[dict[str, Any]] = []
    for message in messages:
        _reject_unsupported(message)
        item = _media_item(message)
        if item["type"] not in ALBUM_TYPES:
            raise ContentError("Этот тип файлов нельзя отправить альбомом.")
        items.append(item)
    return Captured(kind="album", payload={"items": items}, forward_from=_forward_source(messages), buttons=None)


# --------------------------------------------------------------------------- описание поста


def post_text(kind: str, payload: dict[str, Any]) -> str:
    if kind == "text":
        return payload.get("text", "")
    for item in payload.get("items", []):
        if item.get("caption"):
            return item["caption"]
    return ""


def post_title(kind: str, payload: dict[str, Any]) -> str:
    title = KIND_TITLES.get(kind, kind)
    if kind == "album":
        title += f" ({len(payload.get('items', []))})"
    return title


def custom_emoji_count(kind: str, payload: dict[str, Any]) -> int:
    def count(entities: Sequence[dict[str, Any]] | None) -> int:
        return sum(1 for e in entities or [] if e.get("type") == "custom_emoji")

    if kind == "text":
        return count(payload.get("entities"))
    return sum(count(item.get("caption_entities")) for item in payload.get("items", []))


def supports_buttons(kind: str) -> bool:
    return kind != "album"
