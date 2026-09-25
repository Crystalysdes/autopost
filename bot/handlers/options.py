"""Опции рассылки: без звука, защита, закрепление, удаление прошлого, тема форума."""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.states import Input
from bot.ui import screens
from bot.ui import texts as t
from bot.ui.callbacks import Nav, OptAct
from bot.ui.keyboards import btn
from bot.ui.render import finish_input, prompt, show

router = Router(name="options")
router.message.filter(F.chat.type == "private")

TOGGLES = {"silent": "silent", "protect": "protect", "pin": "pin", "delprev": "delete_prev"}
_TOPIC_LINK_RE = re.compile(r"t\.me/(?:c/\d+|[A-Za-z0-9_]{4,})/(\d+)(?:/\d+)?", re.IGNORECASE)


def parse_topic(text: str) -> int | None:
    """«45», «https://t.me/c/123456/45», «t.me/chat/45/100» -> 45. General (1) -> None."""
    value = text.strip()
    if value.isdigit():
        topic = int(value)
    else:
        match = _TOPIC_LINK_RE.search(value)
        if not match:
            raise ValueError("Не нашёл номер темы. Пришлите ссылку на тему или её номер.")
        topic = int(match.group(1))
    return None if topic <= 1 else topic


async def _gone(callback: CallbackQuery, app: App, callback_answer: CallbackAnswer) -> None:
    callback_answer.text = "Это уже удалено"
    await show(app, callback, await screens.main_menu(app))


@router.callback_query(OptAct.filter(F.a.in_(set(TOGGLES))))
async def toggle_option(
    callback: CallbackQuery, callback_data: OptAct, app: App, callback_answer: CallbackAnswer
) -> None:
    campaign = await app.repo.get_campaign(callback_data.id)
    if campaign is None:
        return await _gone(callback, app, callback_answer)
    field = TOGGLES[callback_data.a]
    value = not getattr(campaign, field)
    await app.repo.update_campaign(campaign.id, **{field: value})
    if field == "pin" and value and campaign.chat_id:
        chat = await app.repo.get_chat(campaign.chat_id)
        if chat is not None and not chat.can_pin:
            callback_answer.text = "⚠️ У бота нет права закреплять сообщения в этом чате"
            callback_answer.show_alert = True
    await show(app, callback, await screens.options_view(app, campaign.id))


@router.callback_query(OptAct.filter(F.a == "thread"))
async def ask_thread(
    callback: CallbackQuery,
    callback_data: OptAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    campaign = await app.repo.get_campaign(callback_data.id)
    if campaign is None:
        return await _gone(callback, app, callback_answer)
    text = (
        "🧵 В какую тему форума публиковать?\n\n"
        "Откройте нужную тему → ⋯ → «Копировать ссылку» и пришлите ссылку сюда "
        "(вида <code>https://t.me/c/1234567890/45</code>). Можно прислать просто номер темы."
    )
    await prompt(
        app,
        callback,
        state,
        Input.thread,
        text,
        cancel=Nav(to="opts", id=campaign.id),
        extra=[[btn("General (по умолчанию)", OptAct(a="clrthread", id=campaign.id))]],
        camp_id=campaign.id,
    )


@router.message(Input.thread, F.text)
async def on_thread(message: Message, state: FSMContext, app: App) -> None:
    campaign_id = int((await state.get_data())["camp_id"])
    try:
        topic = parse_topic(message.text)
    except ValueError as error:
        await message.reply(f"⚠️ {t.esc(error)}")
        return
    await app.repo.update_campaign(campaign_id, thread_id=topic)
    await finish_input(app, message, state)
    note = f"✅ Тема: #{topic}" if topic else "✅ Тема: General"
    await show(app, message, await screens.options_view(app, campaign_id, note=note) or await screens.main_menu(app))


@router.callback_query(OptAct.filter(F.a == "clrthread"))
async def clear_thread(
    callback: CallbackQuery,
    callback_data: OptAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    await state.clear()
    campaign = await app.repo.update_campaign(callback_data.id, thread_id=None)
    if campaign is None:
        return await _gone(callback, app, callback_answer)
    await show(app, callback, await screens.options_view(app, campaign.id, note="✅ Тема: General"))
