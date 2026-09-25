"""Экран чата: принять/выйти, права, удаление, новые рассылки."""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.services import chats as chat_service
from bot.ui import keyboards, screens
from bot.ui.callbacks import ApplyDraft, ChatAct
from bot.ui.render import show

router = Router(name="chats")
router.message.filter(F.chat.type == "private")

NEW_CAMPAIGN_NOTE = (
    "👉 Этот чат уже отмечен. Добавьте посты («📝 Посты»), задайте время («⏰ Расписание») и нажмите "
    "«▶️ Запустить». Другие чаты для этой же рассылки — кнопка «💬 Чаты»."
)


async def _gone(callback: CallbackQuery, app: App, callback_answer: CallbackAnswer) -> None:
    callback_answer.text = "Чат уже удалён"
    await show(app, callback, await screens.chats_list(app))


@router.message(F.chat_shared)
async def on_chat_shared(message: Message, app: App) -> None:
    """Админ выбрал чат кнопкой «➕ Добавить…» внизу экрана."""
    shared = message.chat_shared
    try:
        change = await chat_service.fetch_and_apply(app, shared.chat_id, actor_id=message.from_user.id)
    except TelegramAPIError:
        await message.answer(
            "⚠️ Не получилось подключиться к этому чату. Скорее всего, у вас нет права назначать "
            "администраторов, поэтому Telegram не добавил бота.\n\nДобавьте бота администратором вручную "
            "(настройки чата → Администраторы) — чат появится в списке сам.",
            reply_markup=keyboards.add_chat_reply(),
        )
        return
    if change.chat is None or change.status == "left":
        await message.answer("⚠️ Бота нет в этом чате. Добавьте его администратором и попробуйте ещё раз.")
        return
    screen = await screens.chat_view(app, change.chat.id)
    if screen is not None:
        await show(app, message, screen)


async def _not_pending(callback: CallbackQuery, app: App, callback_answer: CallbackAnswer, chat_id: int) -> None:
    """Кнопки «Принять»/«Выйти» из уведомления устарели: чат уже принят (например, другим админом)
    или бота в нём нет. Ничего не делаем, просто показываем чат."""
    chat = await app.repo.get_chat(chat_id)
    callback_answer.text = "Бота уже нет в этом чате" if chat and chat.status == "left" else "Чат уже принят"
    await show(app, callback, await screens.chat_view(app, chat_id) or await screens.chats_list(app))


@router.callback_query(ChatAct.filter(F.a == "accept"))
async def accept_chat(
    callback: CallbackQuery, callback_data: ChatAct, app: App, callback_answer: CallbackAnswer
) -> None:
    chat = await app.repo.get_chat(callback_data.id)
    if chat is None:
        return await _gone(callback, app, callback_answer)
    if chat.status != "pending":
        return await _not_pending(callback, app, callback_answer, chat.id)
    await app.repo.set_chat_status(chat.id, "active")
    callback_answer.text = "✅ Чат принят"
    await show(app, callback, await screens.chat_view(app, chat.id))


@router.callback_query(ChatAct.filter(F.a.in_({"leave", "delleave"})))
async def leave_chat(
    callback: CallbackQuery, callback_data: ChatAct, app: App, callback_answer: CallbackAnswer
) -> None:
    chat = await app.repo.get_chat(callback_data.id)
    if chat is None:
        return await _gone(callback, app, callback_answer)
    # «Выйти» из уведомления работает только для неподтверждённого чата; удаление принятого
    # чата идёт через экран подтверждения («delleave»)
    if callback_data.a == "leave" and chat.status != "pending":
        return await _not_pending(callback, app, callback_answer, chat.id)
    with contextlib.suppress(TelegramAPIError):
        await app.bot.leave_chat(chat.tg_id)
    await app.repo.delete_chat(chat.id)
    callback_answer.text = "🚪 Бот вышел из чата"
    await show(app, callback, await screens.chats_list(app))


@router.callback_query(ChatAct.filter(F.a == "delonly"))
async def delete_chat(
    callback: CallbackQuery, callback_data: ChatAct, app: App, callback_answer: CallbackAnswer
) -> None:
    await app.repo.delete_chat(callback_data.id)
    callback_answer.text = "🗑 Чат удалён из списка"
    await show(app, callback, await screens.chats_list(app))


@router.callback_query(ChatAct.filter(F.a == "del"))
async def confirm_delete(
    callback: CallbackQuery, callback_data: ChatAct, app: App, callback_answer: CallbackAnswer
) -> None:
    screen = await screens.chat_delete_confirm(app, callback_data.id)
    if screen is None:
        return await _gone(callback, app, callback_answer)
    await show(app, callback, screen)


@router.callback_query(ChatAct.filter(F.a == "refresh"))
async def refresh_rights(
    callback: CallbackQuery, callback_data: ChatAct, app: App, callback_answer: CallbackAnswer
) -> None:
    chat = await app.repo.get_chat(callback_data.id)
    if chat is None:
        return await _gone(callback, app, callback_answer)
    change = await chat_service.refresh_chat(app, chat)
    if change is None:
        callback_answer.text = "Не удалось связаться с Telegram, попробуйте позже"
    elif change.status == "left":
        callback_answer.text = "🚫 Бота нет в этом чате"
    else:
        callback_answer.text = "🔄 Права обновлены"
    fresh = await app.repo.get_chat_by_tg(change.chat.tg_id) if change and change.chat else chat
    screen = await screens.chat_view(app, fresh.id if fresh else chat.id)
    await show(app, callback, screen or await screens.chats_list(app))


@router.callback_query(ChatAct.filter(F.a == "new"))
async def new_campaign(
    callback: CallbackQuery, callback_data: ChatAct, app: App, callback_answer: CallbackAnswer
) -> None:
    chat = await app.repo.get_chat(callback_data.id)
    if chat is None:
        return await _gone(callback, app, callback_answer)
    number = await app.repo.count_campaigns() + 1
    campaign = await app.repo.create_campaign(f"Рассылка {number}", chat_ids=[chat.id])
    app.remember_origin(callback.from_user.id, campaign.id, chat.id)
    screen = await screens.campaign_view(app, campaign.id, note=NEW_CAMPAIGN_NOTE, user_id=callback.from_user.id)
    await show(app, callback, screen or await screens.chat_view(app, chat.id))


@router.callback_query(ChatAct.filter(F.a == "fromdraft"))
async def choose_draft(
    callback: CallbackQuery, callback_data: ChatAct, app: App, callback_answer: CallbackAnswer
) -> None:
    screen = await screens.drafts_for_chat(app, callback_data.id)
    if screen is None:
        return await _gone(callback, app, callback_answer)
    await show(app, callback, screen)


@router.callback_query(ApplyDraft.filter())
async def apply_draft(
    callback: CallbackQuery, callback_data: ApplyDraft, app: App, callback_answer: CallbackAnswer
) -> None:
    chat = await app.repo.get_chat(callback_data.chat)
    if chat is None:
        return await _gone(callback, app, callback_answer)
    result = await app.repo.apply_draft(callback_data.draft, [chat.id], activate=False)
    if result is None:
        callback_answer.text = "Черновик уже удалён"
        await show(app, callback, await screens.chat_view(app, chat.id))
        return
    campaign, created = result
    if app.scheduler:
        await app.scheduler.reschedule([campaign.id])
    if created:
        note = "✅ Из черновика создана рассылка с этим чатом. Проверьте её и нажмите «▶️ Запустить»."
    else:
        note = (
            "🔄 Рассылка из этого черновика уже была — чат добавлен в неё, а посты, расписание и опции "
            "обновлены по черновику."
        )
    app.remember_origin(callback.from_user.id, campaign.id, chat.id)
    screen = await screens.campaign_view(app, campaign.id, note=note, user_id=callback.from_user.id)
    await show(app, callback, screen or await screens.chat_view(app, chat.id))


@router.callback_query(ChatAct.filter(F.a == "resume"))
async def resume_chat(
    callback: CallbackQuery, callback_data: ChatAct, app: App, callback_answer: CallbackAnswer
) -> None:
    """Снимает паузу после ошибок со всех рассылок этого чата."""
    chat = await app.repo.get_chat(callback_data.id)
    if chat is None:
        return await _gone(callback, app, callback_answer)
    if chat.status != "active" or not chat.can_post:
        callback_answer.text = "Бот не может публиковать в этом чате — сначала верните ему права"
        callback_answer.show_alert = True
        return
    resumed = await app.repo.resume_chat_targets(chat.id)
    callback_answer.text = f"▶️ Возобновлено в рассылках: {resumed}" if resumed else "Паузы уже нет"
    await show(app, callback, await screens.chat_view(app, chat.id))
