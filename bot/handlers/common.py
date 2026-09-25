"""Команды, главное меню и навигация между экранами."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.ui import keyboards, screens
from bot.ui.callbacks import Nav
from bot.ui.render import show

logger = logging.getLogger(__name__)

router = Router(name="common")
router.message.filter(F.chat.type == "private")

COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="chats", description="Мои чаты"),
    BotCommand(command="drafts", description="Черновики"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="cancel", description="Отменить ввод"),
]


async def setup_commands(bot: Bot, owner_id: int) -> bool:
    """Меню команд видно только владельцу; остальным бот ничего не показывает."""
    try:
        await bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
        return True
    except TelegramAPIError as error:
        logger.info("Команды пока не установлены (%s) — повторю после /start", error)
        return False


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, app: App) -> None:
    await state.clear()
    if not app.commands_ready:
        app.commands_ready = await setup_commands(app.bot, app.owner_id)
    await message.answer(
        "👋 Панель автопостинга. Кнопки внизу экрана добавляют бота в группу или канал.",
        reply_markup=keyboards.add_chat_reply(),
    )
    await show(app, message, await screens.main_menu(app))


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext, app: App) -> None:
    await state.clear()
    await show(app, message, await screens.main_menu(app))


@router.message(Command("chats"))
async def cmd_chats(message: Message, state: FSMContext, app: App) -> None:
    await state.clear()
    await show(app, message, await screens.chats_list(app))


@router.message(Command("drafts"))
async def cmd_drafts(message: Message, state: FSMContext, app: App) -> None:
    await state.clear()
    await show(app, message, await screens.drafts_list(app))


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext, app: App) -> None:
    await state.clear()
    await show(app, message, await screens.settings_view(app))


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext, app: App) -> None:
    await state.clear()
    await show(app, message, screens.help_view())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, app: App) -> None:
    current = await state.get_state()
    await state.clear()
    await message.answer("Ввод отменён." if current else "Сейчас нечего отменять.")
    await show(app, message, await screens.main_menu(app))


@router.callback_query(Nav.filter())
async def on_nav(
    callback: CallbackQuery, callback_data: Nav, state: FSMContext, app: App, callback_answer: CallbackAnswer
) -> None:
    await state.clear()
    screen = await screens.resolve(app, callback_data)
    if screen is None:
        callback_answer.text = "Это уже удалено"
        screen = await screens.main_menu(app)
    await show(app, callback, screen)
