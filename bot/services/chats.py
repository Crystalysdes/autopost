"""Регистрация чатов, обновление прав бота и миграция групп в супергруппы."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
)

from bot.app import App
from bot.db.models import Chat
from bot.db.repo import ChatChange
from bot.services.rights import enum_text, rights_from_member
from bot.ui import texts

logger = logging.getLogger(__name__)

_GONE_MARKERS = ("chat not found", "not a member", "bot was kicked")


async def apply_member(app: App, *, chat: Any, member: Any, actor_id: int | None) -> ChatChange:
    """chat — Chat/ChatFullInfo из Telegram, member — права бота в нём."""
    chat_type = enum_text(chat.type)
    rights = rights_from_member(chat_type, member)
    return await app.repo.upsert_chat_membership(
        tg_id=chat.id,
        title=chat.title or "",
        username=chat.username,
        chat_type=chat_type,
        is_forum=bool(getattr(chat, "is_forum", False)),
        in_chat=rights.in_chat,
        is_admin=rights.is_admin,
        can_post=rights.can_post,
        can_pin=rights.can_pin,
        actor_id=actor_id,
        owner_id=app.owner_id,
    )


async def fetch_and_apply(app: App, tg_id: int, *, actor_id: int | None) -> ChatChange:
    info = await app.bot.get_chat(tg_id)
    member = await app.bot.get_chat_member(tg_id, app.bot_id)
    return await apply_member(app, chat=info, member=member, actor_id=actor_id)


async def migrate(app: App, old_tg_id: int, new_tg_id: int) -> Chat | None:
    chat = await app.repo.migrate_chat(old_tg_id, new_tg_id)
    if chat is not None:
        logger.info("Чат %s стал супергруппой %s", old_tg_id, new_tg_id)
        try:
            await fetch_and_apply(app, new_tg_id, actor_id=None)
        except TelegramAPIError as error:
            logger.warning("Не удалось обновить права после миграции: %s", error)
    return chat


async def refresh_chat(app: App, chat: Chat) -> ChatChange | None:
    """Перечитывает название и права бота. None — не удалось (например, нет сети)."""
    try:
        return await fetch_and_apply(app, chat.tg_id, actor_id=None)
    except TelegramMigrateToChat as error:
        await migrate(app, chat.tg_id, error.migrate_to_chat_id)
        return await fetch_and_apply(app, error.migrate_to_chat_id, actor_id=None)
    except (TelegramForbiddenError, TelegramBadRequest) as error:
        if isinstance(error, TelegramBadRequest) and not any(
            marker in error.message.lower() for marker in _GONE_MARKERS
        ):
            logger.warning("Не удалось проверить чат %s: %s", chat.tg_id, error.message)
            return None
        updated = await app.repo.set_chat_status(chat.id, "left")
        return ChatChange(chat=updated or chat, prev_status=chat.status, status="left")
    except TelegramAPIError as error:
        logger.warning("Не удалось проверить чат %s: %s", chat.tg_id, error)
        return None


async def refresh_all(app: App, *, delay: float = 0.3) -> None:
    """Фоновая проверка прав во всех чатах при запуске."""
    for chat in await app.repo.list_chats(statuses=("active", "pending")):
        change = await refresh_chat(app, chat)
        if change is not None and change.status == "left" and change.prev_status == "active":
            await app.notify(texts.chat_lost_text(chat.title, "бот больше не состоит в чате"))
        await asyncio.sleep(delay)
