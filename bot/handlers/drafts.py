"""Выбор чатов с галочками: применение черновика и копирование рассылки."""

from __future__ import annotations

from typing import Any

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.services.campaigns import schedule_problems
from bot.states import Picker
from bot.ui import screens
from bot.ui import texts as t
from bot.ui.callbacks import PickAct
from bot.ui.render import show

router = Router(name="drafts")


async def _eligible_chat_ids(app: App, data: dict[str, Any]) -> list[int]:
    source = await app.repo.get_campaign(int(data["src"]))
    chats = await app.repo.list_chats(statuses=("active",))
    exclude = source.chat_id if source and data["mode"] == "copy" else None
    return [c.id for c in chats if c.id != exclude]


@router.callback_query(PickAct.filter(), Picker.picking)
async def on_pick(
    callback: CallbackQuery,
    callback_data: PickAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    data = await state.get_data()
    if not data.get("src"):
        await state.clear()
        callback_answer.text = "Выбор устарел — откройте его заново"
        return
    selected = {int(x) for x in data.get("sel", [])}
    action = callback_data.a
    if action == "go":
        await _apply(callback, data, bool(callback_data.v), state, app, callback_answer)
        return
    if action == "t":
        selected ^= {callback_data.v}
    elif action == "pg":
        data["page"] = callback_data.v
    elif action == "all":
        selected = set(await _eligible_chat_ids(app, data))
    elif action == "none":
        selected = set()
    data["sel"] = sorted(selected)
    await state.set_data(data)
    screen = await screens.picker_view(app, data)
    if screen is None:
        await state.clear()
        callback_answer.text = "Источник уже удалён"
        await show(app, callback, await screens.main_menu(app))
        return
    await show(app, callback, screen)


@router.callback_query(PickAct.filter())
async def stale_pick(callback: CallbackQuery, callback_answer: CallbackAnswer) -> None:
    callback_answer.text = "Выбор устарел — откройте его заново"
    callback_answer.show_alert = True


async def _apply(
    callback: CallbackQuery,
    data: dict[str, Any],
    activate: bool,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    source = await app.repo.get_campaign(int(data["src"]))
    if source is None:
        await state.clear()
        callback_answer.text = "Источник уже удалён"
        await show(app, callback, await screens.main_menu(app))
        return
    eligible = set(await _eligible_chat_ids(app, data))
    selected = [chat_id for chat_id in data.get("sel", []) if chat_id in eligible]
    if not selected:
        callback_answer.text = "Отметьте хотя бы один чат"
        callback_answer.show_alert = True
        return
    posts = await app.repo.list_posts(source.id)
    now = app.scheduler.now() if app.scheduler else 0
    if activate:
        problems = schedule_problems(source, posts, app.settings.tz, now)
        if problems:
            callback_answer.text = problems[0] + " — или примените на паузе"
            callback_answer.show_alert = True
            return

    results = await app.repo.apply_template(source.id, selected, activate=activate, link=data["mode"] == "draft")
    chats = {c.id: c for c in await app.repo.list_chats()}
    warnings: list[str] = []
    without_rights = [c.id for c, _ in results if not chats[c.chat_id].can_post]
    if without_rights:
        for campaign_id in without_rights:
            await app.repo.update_campaign(campaign_id, is_active=False)
        names = ", ".join(t.esc(chats[c.chat_id].title, 30) for c, _ in results if c.id in without_rights)
        warnings.append(f"⚠️ Нет права публиковать, рассылки оставлены на паузе: {names}")
    if source.pin:
        no_pin = [t.esc(chats[c.chat_id].title, 30) for c, _ in results if not chats[c.chat_id].can_pin]
        if no_pin:
            warnings.append("📌 Нет права закреплять: " + ", ".join(no_pin))
    if app.scheduler:
        await app.scheduler.reschedule([c.id for c, _ in results])
    await state.clear()
    await show(
        app,
        callback,
        await screens.apply_summary(app, source.id, [(c.id, created) for c, created in results], warnings),
    )
