"""Опции рассылки: без звука, защита, закрепление, удаление прошлого. Тема форума — в «Чаты рассылки»."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.ui import screens
from bot.ui.callbacks import OptAct
from bot.ui.render import show

router = Router(name="options")
router.message.filter(F.chat.type == "private")

TOGGLES = {"silent": "silent", "protect": "protect", "pin": "pin", "delprev": "delete_prev"}


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
    value = bool(callback_data.v)
    await app.repo.update_campaign(campaign.id, **{field: value})
    if field == "pin" and value and not campaign.is_draft:
        targets = await app.repo.campaign_targets(campaign.id)
        if any(x.chat.status == "active" and not x.chat.can_pin for x in targets):
            callback_answer.text = "⚠️ В некоторых чатах рассылки у бота нет права закреплять сообщения"
            callback_answer.show_alert = True
    await show(app, callback, await screens.options_view(app, campaign.id))
