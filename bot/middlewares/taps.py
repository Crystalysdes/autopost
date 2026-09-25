"""Обработка нажатий кнопок: защита от двойных тапов и сброс незаконченного ввода."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, TelegramObject

from bot.ui.callbacks import PickAct


class DoubleTapMiddleware(BaseMiddleware):
    """Повторное нажатие той же кнопки, пока первое ещё обрабатывается или в течение
    ``window`` секунд после него, игнорируется. Иначе двойной тап создавал бы две рассылки
    или отправлял два поста."""

    def __init__(self, window: float = 1.5) -> None:
        self.window = window
        self._busy: set[tuple[int, str]] = set()
        self._finished: dict[tuple[int, str], float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery) or not event.data:
            return await handler(event, data)
        key = (event.from_user.id, event.data)
        now = time.monotonic()
        if key in self._busy or now - self._finished.get(key, float("-inf")) < self.window:
            with contextlib.suppress(TelegramAPIError):
                await event.answer("⏳")
            return None
        self._busy.add(key)
        try:
            return await handler(event, data)
        finally:
            self._busy.discard(key)
            finished = time.monotonic()
            self._finished[key] = finished
            if len(self._finished) > 200:
                self._finished = {k: v for k, v in self._finished.items() if finished - v < self.window}


class ResetInputMiddleware(BaseMiddleware):
    """Нажатие любой кнопки (кроме галочек выбора чатов) завершает незаконченный ввод.
    Иначе, например, после «➕ Новая рассылка» следующие сообщения ушли бы постами в старую."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state: FSMContext | None = data.get("state")
        if (
            state is not None
            and isinstance(event, CallbackQuery)
            and not (event.data or "").startswith(PickAct.__prefix__ + ":")
        ):
            await state.clear()
        return await handler(event, data)
