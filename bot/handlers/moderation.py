"""Участники групп и каналов: антиспам, обязательная подписка и автоприём заявок
(см. bot/services/moderation.py)."""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, ChatJoinRequest, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.services import group_commands
from bot.ui.callbacks import SubCheck

router = Router(name="moderation")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))
router.edited_message.filter(F.chat.type.in_({"group", "supergroup"}))


@router.message(Command(*group_commands.COMMANDS, ignore_case=True))
async def on_admin_command(message: Message, command: CommandObject, app: App) -> None:
    """/ban, /mute, /del и другие — ответом на сообщение (bot/services/group_commands.py)."""
    await group_commands.handle(app, message, command)


@router.message()
async def on_group_message(message: Message, app: App) -> None:
    if app.moderator is not None:
        await app.moderator.handle(message)


@router.edited_message()
async def on_group_edit(message: Message, app: App) -> None:
    """Спамеры пишут безобидный текст, а ссылку добавляют правкой."""
    if app.moderator is not None:
        await app.moderator.handle(message, edited=True)


@router.chat_join_request()
async def on_join_request(request: ChatJoinRequest, app: App) -> None:
    """Заявка на вступление в группу или канал (приходит, только если у бота есть право приглашать)."""
    if app.moderator is not None:
        await app.moderator.join_request(request)


@router.callback_query(SubCheck.filter())
async def on_sub_check(
    callback: CallbackQuery, callback_data: SubCheck, app: App, callback_answer: CallbackAnswer
) -> None:
    """«✅ Проверить подписку». Кнопку видят все, но работает она только для того, к кому обращается
    подсказка. Админам она показывает, подписан ли он; остальным — что кнопка не для них."""
    notice = callback.message
    moderator = app.moderator
    if moderator is None or not isinstance(notice, Message):
        callback_answer.text = "Подсказка устарела — просто напишите сообщение ещё раз"
        return
    info = await moderator.chat_info(notice.chat.id)
    user = callback.from_user
    if user.id != callback_data.u:
        if info is None or not await moderator.is_moderator(info, user):
            callback_answer.text = "🔒 Эта кнопка — для участника, к которому обращается подсказка"
            callback_answer.show_alert = True
            return
        # Админ смотрит, подписался ли адресат. Писать тот сможет, только подписавшись сам.
        missing = await moderator.missing_channels(callback_data.u, info.channels, fresh=True)
        callback_answer.show_alert = True
        if missing:
            titles = ", ".join(f"«{c.title}»" for c in missing)
            status = f"Участник ещё не подписан на: {titles} — писать не сможет, пока не подпишется."
            callback_answer.text = status[:200]
            return
        callback_answer.text = "✅ Участник уже подписан и может писать — подсказку убрал"
        moderator.notice_done(notice.chat.id, callback_data.u)
        with contextlib.suppress(TelegramAPIError):
            await notice.delete()
        return

    missing = await moderator.missing_channels(user.id, info.channels, fresh=True) if info else []
    if missing:
        refreshed = info is not None and await moderator.refresh_notice(info, notice, user, missing)
        callback_answer.text = (
            "Вы ещё не подписаны на: "
            + ", ".join(f"«{c.title}»" for c in missing)
            + ". Подпишитесь по кнопке в подсказке и нажмите «Проверить подписку» ещё раз."
            + (" Ссылки в подсказке обновлены." if refreshed else "")
        )[:200]
        callback_answer.show_alert = True
        return
    callback_answer.text = "✅ Спасибо! Теперь можно писать"
    moderator.notice_done(notice.chat.id, user.id)
    with contextlib.suppress(TelegramAPIError):
        await notice.delete()
