"""Всё, что не подошло ни одному обработчику. Подключается последним."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.ui import screens
from bot.ui.render import show

router = Router(name="fallback")
router.message.filter(F.chat.type == "private")


@router.message(StateFilter(None))
async def unknown_message(message: Message, app: App) -> None:
    await message.answer(
        "Не понял 🤔 Управление — кнопками меню.\n"
        "Чтобы добавить пост: Мои чаты → чат → рассылка → «📝 Посты» → «➕ Добавить посты»."
    )
    await show(app, message, await screens.main_menu(app))


@router.message()
async def wrong_input(message: Message) -> None:
    await message.reply("Здесь нужен текст. Пришлите сообщение в указанном формате или нажмите «✖️ Отмена».")


@router.callback_query()
async def stale_callback(callback: CallbackQuery, callback_answer: CallbackAnswer) -> None:
    callback_answer.text = "Кнопка устарела — откройте меню заново: /start"
