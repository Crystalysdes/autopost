"""Данные inline-кнопок. Только короткие строки и целые числа — лимит Telegram 64 байта.

Всё, что нужно для обработки нажатия, лежит в самих данных, поэтому кнопки
продолжают работать и после перезапуска бота.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class Nav(CallbackData, prefix="n"):
    """Переход на экран. Любая навигация сбрасывает незавершённый ввод."""

    to: str
    id: int = 0
    page: int = 0


class ChatAct(CallbackData, prefix="c"):
    a: str
    id: int


class CampAct(CallbackData, prefix="k"):
    a: str
    id: int


class PostAct(CallbackData, prefix="p"):
    a: str
    id: int


class SchedAct(CallbackData, prefix="s"):
    a: str
    id: int
    v: int = 0
    w: int = 0


class OptAct(CallbackData, prefix="o"):
    """v — значение, которое кнопка устанавливает (а не «переключить»): старая кнопка
    из другой панели не должна выключать то, что уже включено."""

    a: str
    id: int
    v: int = 0


class ApplyDraft(CallbackData, prefix="a"):
    chat: int
    draft: int


class PickAct(CallbackData, prefix="x"):
    """Выбор чатов с галочками. s — id источника: кнопки старого окна выбора не должны
    срабатывать для нового."""

    a: str
    s: int
    v: int = 0


class SetAct(CallbackData, prefix="g"):
    a: str
    v: int = 0
