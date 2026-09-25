"""Отправка поста в чат.

Посты всегда отправляются с явными entities и parse_mode=None — в том числе в каждом
элементе альбома. Иначе aiogram подставил бы parse_mode интерфейса (HTML) поверх entities.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaLivePhoto,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from bot.services.buttons import Rows, buttons_to_markup, has_icons
from bot.services.content import CAPTION_TYPES, SPOILER_TYPES, load_entities, load_link_preview

logger = logging.getLogger(__name__)

_SEND_METHODS = {
    "photo": "send_photo",
    "video": "send_video",
    "animation": "send_animation",
    "document": "send_document",
    "audio": "send_audio",
    "voice": "send_voice",
    "video_note": "send_video_note",
    "sticker": "send_sticker",
    "live_photo": "send_live_photo",
}
_INPUT_MEDIA = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "animation": InputMediaAnimation,
    "document": InputMediaDocument,
    "audio": InputMediaAudio,
    "live_photo": InputMediaLivePhoto,
}


class PostSendError(Exception):
    """Пост невозможно отправить из-за его содержимого (текст ошибки — для админа)."""


@dataclass
class PostData:
    """Снимок поста, не привязанный к БД."""

    id: int | None
    kind: str
    payload: dict[str, Any]
    buttons: Rows | None = None
    forward_from: dict[str, Any] | None = None
    send_mode: str = "copy"

    @classmethod
    def of(cls, post: Any) -> PostData:
        return cls(
            id=post.id,
            kind=post.kind,
            payload=post.payload,
            buttons=post.buttons,
            forward_from=post.forward_from,
            send_mode=post.send_mode,
        )

    @property
    def is_forward(self) -> bool:
        return self.send_mode == "forward" and bool(self.forward_from)


@dataclass
class SendResult:
    message_ids: list[int]
    messages: list[Message] = field(default_factory=list)
    forwarded: bool = False
    icons_dropped: bool = False

    @property
    def usable_ids(self) -> list[int]:
        # id=0 — сервер отложил отправку (например, видео в большой чат); с ним ничего нельзя сделать
        return [i for i in self.message_ids if i]


def _is_icon_error(error: TelegramBadRequest) -> bool:
    text = error.message.lower()
    return "emoji" in text or "icon" in text


async def send_post(
    bot: Bot,
    chat_id: int,
    post: PostData,
    *,
    silent: bool = False,
    protect: bool = False,
    thread_id: int | None = None,
) -> SendResult:
    try:
        return await _send(bot, chat_id, post, silent, protect, thread_id, with_icons=True)
    except TelegramBadRequest as error:
        # Иконки-премиум-эмодзи на кнопках доступны не всем ботам — пробуем без них.
        if post.buttons and has_icons(post.buttons) and _is_icon_error(error):
            logger.warning("Кнопки с иконками отклонены (%s), отправляю без иконок", error.message)
            result = await _send(bot, chat_id, post, silent, protect, thread_id, with_icons=False)
            result.icons_dropped = True
            return result
        raise


async def _send(
    bot: Bot,
    chat_id: int,
    post: PostData,
    silent: bool,
    protect: bool,
    thread_id: int | None,
    *,
    with_icons: bool,
) -> SendResult:
    common: dict[str, Any] = {
        "chat_id": chat_id,
        "disable_notification": silent,
        "protect_content": protect,
        "message_thread_id": thread_id,
    }

    if post.is_forward:
        source = post.forward_from or {}
        forwarded = await bot.forward_messages(
            from_chat_id=source["chat_id"], message_ids=list(source["message_ids"]), **common
        )
        if not forwarded:
            raise PostSendError(
                "Исходное сообщение для пересылки не найдено — возможно, его удалили из чата с ботом. "
                "Замените пост или переключите его в режим копии."
            )
        return SendResult(message_ids=[m.message_id for m in forwarded], forwarded=True)

    payload = post.payload
    if post.kind == "album":
        media = [_input_media(item) for item in payload["items"]]
        messages = await bot.send_media_group(media=media, **common)
        return SendResult(message_ids=[m.message_id for m in messages], messages=list(messages))

    markup = buttons_to_markup(post.buttons, with_icons=with_icons)
    if post.kind == "text":
        message = await bot.send_message(
            text=payload["text"],
            entities=load_entities(payload.get("entities")),
            link_preview_options=load_link_preview(payload.get("link_preview")),
            parse_mode=None,
            reply_markup=markup,
            **common,
        )
        return SendResult(message_ids=[message.message_id], messages=[message])

    item = payload["items"][0]
    kind = item["type"]
    method = getattr(bot, _SEND_METHODS[kind])
    kwargs: dict[str, Any] = {kind: item["file_id"], "reply_markup": markup, **common}
    if kind == "live_photo":
        kwargs["photo"] = item["photo_file_id"]
    if kind in CAPTION_TYPES:
        kwargs["caption"] = item.get("caption")
        kwargs["caption_entities"] = load_entities(item.get("caption_entities"))
        kwargs["parse_mode"] = None
    if kind in SPOILER_TYPES:
        kwargs["has_spoiler"] = bool(item.get("has_spoiler"))
        kwargs["show_caption_above_media"] = bool(item.get("show_caption_above_media"))
    message = await method(**kwargs)
    return SendResult(message_ids=[message.message_id], messages=[message])


def _input_media(item: dict[str, Any]) -> Any:
    kind = item["type"]
    kwargs: dict[str, Any] = {
        "media": item["file_id"],
        "caption": item.get("caption"),
        "caption_entities": load_entities(item.get("caption_entities")),
        "parse_mode": None,
    }
    if kind == "live_photo":
        kwargs["photo"] = item["photo_file_id"]
    if kind in SPOILER_TYPES:
        kwargs["has_spoiler"] = bool(item.get("has_spoiler"))
        kwargs["show_caption_above_media"] = bool(item.get("show_caption_above_media"))
    return _INPUT_MEDIA[kind](**kwargs)


def received_custom_emoji(messages: list[Message]) -> int:
    total = 0
    for message in messages:
        for entity in (message.entities or []) + (message.caption_entities or []):
            if entity.type == "custom_emoji":
                total += 1
    return total
