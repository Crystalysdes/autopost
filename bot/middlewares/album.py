"""Сборка альбомов: Telegram присылает каждый файл альбома отдельным сообщением."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject


class AlbumMiddleware(BaseMiddleware):
    """Первое сообщение альбома ждёт, пока новые части перестанут приходить (latency секунд
    тишины), и вызывает обработчик один раз со списком ``album``. Остальные части поглощаются.

    Работает, потому что aiogram обрабатывает апдейты параллельно (handle_as_tasks=True).
    """

    def __init__(self, latency: float = 0.8) -> None:
        self.latency = latency
        self._albums: dict[str, list[Message]] = {}
        self._last_seen: dict[str, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.media_group_id or event.chat.type != "private":
            return await handler(event, data)

        loop = asyncio.get_running_loop()
        key = f"{event.chat.id}:{event.media_group_id}"
        if key in self._albums:
            self._albums[key].append(event)
            self._last_seen[key] = loop.time()
            return None

        self._albums[key] = [event]
        self._last_seen[key] = loop.time()
        while True:
            await asyncio.sleep(self.latency)
            if loop.time() - self._last_seen[key] >= self.latency:
                break
        album = sorted(self._albums.pop(key), key=lambda m: m.message_id)
        self._last_seen.pop(key, None)
        data["album"] = album
        return await handler(album[0], data)
