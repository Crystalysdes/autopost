"""Данные inline-кнопок. Только короткие строки и целые числа — лимит Telegram 64 байта.

Всё, что нужно для обработки нажатия, лежит в самих данных, поэтому кнопки
продолжают работать и после перезапуска бота.
"""

from __future__ import annotations

from typing import Any

from aiogram.filters.callback_data import CallbackData
from pydantic_core import PydanticUndefined


class _OldButtons:
    """Кнопки, отправленные прошлой версией бота, приходят без полей, добавленных позже в конец.
    Недостающие поля получают значения по умолчанию — иначе старые панели и уведомления
    в чате с ботом перестали бы работать после обновления."""

    @classmethod
    def unpack(cls, value: str) -> Any:
        separator = cls.__separator__  # type: ignore[attr-defined]
        parts = value.split(separator)
        fields = list(cls.model_fields.values())  # type: ignore[attr-defined]
        missing = fields[len(parts) - 1 :]
        if 0 < len(missing) < len(fields) and all(f.default is not PydanticUndefined for f in missing):
            value = separator.join([*parts, *(str(f.default) for f in missing)])
        return super().unpack(value)  # type: ignore[misc]


class Nav(_OldButtons, CallbackData, prefix="n"):
    """Переход на экран. Любая навигация сбрасывает незавершённый ввод.

    f — откуда пришли. Для рассылки: id чата (кнопка «Назад» вернёт в него), -1 — из списков
    (назад — к списку рассылок), 0 — оставить как было. Для поста: страница «Моих постов» + 1.
    """

    to: str
    id: int = 0
    page: int = 0
    f: int = 0


class ChatAct(CallbackData, prefix="c"):
    a: str
    id: int


class CampAct(CallbackData, prefix="k"):
    a: str
    id: int


class PostAct(_OldButtons, CallbackData, prefix="p"):
    """f — пост открыт из «Моих постов» (страница + 1), 0 — из постов рассылки."""

    a: str
    id: int
    f: int = 0


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


class TargetAct(CallbackData, prefix="t"):
    """Чаты рассылки. Каждое нажатие сразу сохраняется, значение приходит в самой кнопке:
    id — рассылка, v — чат (или номер страницы), p — страница, на которой нажали."""

    a: str
    id: int
    v: int = 0
    p: int = 0


class LibAct(CallbackData, prefix="l"):
    """Действия с постом из «Моих постов»: id — пост, v — рассылка или страница, f — как у PostAct."""

    a: str
    id: int
    v: int = 0
    f: int = 0
