"""Показ экранов: редактируем «панель» на месте, а если нельзя — присылаем новое сообщение."""

from __future__ import annotations

import contextlib
import logging

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.app import NO_PREVIEW, App
from bot.ui.callbacks import Nav
from bot.ui.keyboards import btn, markup

logger = logging.getLogger(__name__)

Screen = tuple[str, InlineKeyboardMarkup]


async def show(app: App, event: Message | CallbackQuery, screen: Screen, *, new: bool = False) -> int | None:
    """Возвращает id сообщения с экраном."""
    text, keyboard = screen
    if isinstance(event, CallbackQuery):
        chat_id = event.from_user.id
        message = event.message
        # Старые сообщения приходят как InaccessibleMessage — их можно только заменить новым
        if not new and isinstance(message, Message):
            try:
                await app.bot.edit_message_text(
                    text=text,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=keyboard,
                    link_preview_options=NO_PREVIEW,
                )
                return message.message_id
            except TelegramBadRequest as error:
                if "not modified" in error.message:
                    return message.message_id
                logger.debug("Не удалось отредактировать панель: %s", error.message)
    else:
        chat_id = event.chat.id
    sent = await app.bot.send_message(chat_id, text, reply_markup=keyboard, link_preview_options=NO_PREVIEW)
    return sent.message_id


async def prompt(
    app: App,
    event: Message | CallbackQuery,
    state: FSMContext,
    new_state: State,
    text: str,
    *,
    cancel: Nav,
    extra: list[list[InlineKeyboardButton]] | None = None,
    **data: object,
) -> None:
    """Просит пользователя что-то прислать. Кнопка «Отмена» — навигация (она сбрасывает ввод)."""
    rows = [*(extra or []), [btn("✖️ Отмена", cancel)]]
    message_id = await show(app, event, (text, markup(*rows)))
    await state.set_state(new_state)
    await state.update_data(prompt_id=message_id, **data)


async def finish_input(app: App, message: Message, state: FSMContext) -> None:
    """Ввод принят: сбрасываем состояние и убираем сообщение-подсказку, чтобы не мусорить."""
    data = await state.get_data()
    await state.clear()
    prompt_id = data.get("prompt_id")
    if prompt_id:
        with contextlib.suppress(TelegramAPIError):
            await app.bot.delete_message(message.chat.id, int(prompt_id))
