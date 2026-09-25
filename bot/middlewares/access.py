"""Доступ только админам (ADMIN_IDS)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

logger = logging.getLogger(__name__)


class AdminOnlyMiddleware(BaseMiddleware):
    """Сообщения в личке и нажатия кнопок принимаются только от админов.

    Сообщения из групп пропускаются дальше: там обрабатываются только служебные события
    (миграция группы в супергруппу), всё остальное отбрасывается роутерами.
    """

    def __init__(self, admin_ids: Iterable[int]) -> None:
        self.admin_ids = frozenset(admin_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.chat.type != "private":
            return await handler(event, data)
        user = event.from_user if isinstance(event, (Message, CallbackQuery)) else None
        if user is None or user.id not in self.admin_ids:
            logger.info("Игнорирую апдейт от постороннего пользователя %s", user.id if user else "?")
            return None
        return await handler(event, data)
