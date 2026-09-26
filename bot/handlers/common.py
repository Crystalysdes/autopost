"""Команды, главное меню и навигация между экранами."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CallbackQuery,
    Message,
)
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
    BotCommand(command="campaigns", description="Рассылки"),
    BotCommand(command="posts", description="Мои посты"),
    BotCommand(command="chats", description="Мои чаты"),
    BotCommand(command="drafts", description="Черновики"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="cancel", description="Отменить ввод"),
]
# Меню в группах — только для их админов (bot/services/group_commands.py)
GROUP_COMMANDS = [
    BotCommand(command="del", description="Удалить сообщение (ответом на него)"),
    BotCommand(command="mute", description="Замутить: /mute, /mute 1h, /mute 1day (ответом)"),
    BotCommand(command="delmute", description="Удалить сообщение и замутить автора"),
    BotCommand(command="ban", description="Забанить: /ban или /ban 1day (ответом)"),
    BotCommand(command="delban", description="Удалить сообщение и забанить автора"),
    BotCommand(command="unmute", description="Снять мут (ответом или /unmute ID)"),
    BotCommand(command="unban", description="Разбанить (ответом или /unban ID)"),
]


async def setup_commands(bot: Bot, admin_ids: Iterable[int]) -> set[int]:
    """Меню команд видно только админам; остальным бот ничего не показывает.
    Возвращает админов, которым меню установлено (остальным — после их /start)."""
    ready: set[int] = set()
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
            ready.add(admin_id)
        except TelegramAPIError as error:
            logger.info("Меню команд для %s пока не установлено (%s) — повторю после /start", admin_id, error)
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
    except TelegramAPIError as error:
        logger.info("Не удалось очистить общее меню команд: %s", error)
    try:
        await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllChatAdministrators())
    except TelegramAPIError as error:
        logger.info("Не удалось установить команды для админов групп: %s", error)
    return ready


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, app: App) -> None:
    await state.clear()
    if message.from_user.id not in app.commands_ready:
        app.commands_ready |= await setup_commands(app.bot, [message.from_user.id])
    await message.answer(
        "👋 Панель автопостинга. Кнопки внизу экрана добавляют бота в группу или канал, а также скрытых админов.",
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


@router.message(Command("campaigns"))
async def cmd_campaigns(message: Message, state: FSMContext, app: App) -> None:
    await state.clear()
    await show(app, message, await screens.campaigns_list(app))


@router.message(Command("posts"))
async def cmd_posts(message: Message, state: FSMContext, app: App) -> None:
    await state.clear()
    await show(app, message, await screens.library_view(app))


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
    if callback_data.to == "camp" and callback_data.f:
        app.remember_origin(callback.from_user.id, callback_data.id, callback_data.f)
    screen = await screens.resolve(app, callback_data, callback.from_user.id)
    if screen is None:
        callback_answer.text = "Это уже удалено"
        screen = await screens.main_menu(app)
    await show(app, callback, screen)
