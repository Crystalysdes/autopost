"""Доступ только владельцу (ADMIN_ID)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    """Сообщения в личке и нажатия кнопок принимаются только от владельца.

    Сообщения из групп пропускаются дальше: там обрабатываются только служебные события
    (миграция группы в супергруппу), всё остальное отбрасывается роутерами.
    """

    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.chat.type != "private":
            return await handler(event, data)
        user = event.from_user if isinstance(event, (Message, CallbackQuery)) else None
        if user is None or user.id != self.owner_id:
            logger.info("Игнорирую апдейт от постороннего пользователя %s", user.id if user else "?")
            return None
        return await handler(event, data)
