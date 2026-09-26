"""Участники групп и каналов: антиспам, обязательная подписка и автоприём заявок
(см. bot/services/moderation.py)."""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, ChatJoinRequest, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.ui.callbacks import SubCheck

router = Router(name="moderation")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))
router.edited_message.filter(F.chat.type.in_({"group", "supergroup"}))


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
    """«✅ Проверить подписку»: проверяет того, кто нажал, без кэша. Подписан — подсказка исчезает
    (если она адресована ему) и можно писать."""
    notice = callback.message
    if app.moderator is None or not isinstance(notice, Message):
        callback_answer.text = "Подсказка устарела — просто напишите сообщение ещё раз"
        return
    info = await app.moderator.chat_info(notice.chat.id)
    missing = await app.moderator.missing_channels(callback.from_user.id, info.channels, fresh=True) if info else []
    if missing:
        callback_answer.text = (
            "Вы ещё не подписаны на: "
            + ", ".join(f"«{c.title}»" for c in missing)
            + ". Подпишитесь по кнопке в подсказке и нажмите «Проверить подписку» ещё раз."
        )[:200]
        callback_answer.show_alert = True
        return
    callback_answer.text = "✅ Спасибо! Теперь можно писать"
    if callback.from_user.id == callback_data.u:
        app.moderator.notice_done(notice.chat.id, callback.from_user.id)
        with contextlib.suppress(TelegramAPIError):
            await notice.delete()
