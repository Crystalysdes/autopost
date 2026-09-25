"""Выбор чатов с галочками для применения черновика."""

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


async def _eligible_chat_ids(app: App) -> list[int]:
    return [c.id for c in await app.repo.list_chats(statuses=("active",))]


@router.callback_query(PickAct.filter(), Picker.picking)
async def on_pick(
    callback: CallbackQuery,
    callback_data: PickAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    data = await state.get_data()
    if not data.get("src") or int(data["src"]) != callback_data.s:
        callback_answer.text = "Это окно выбора устарело — откройте его заново"
        callback_answer.show_alert = True
        return
    selected = {int(x) for x in data.get("sel", [])}
    action = callback_data.a
    if action == "go":
        # Сбрасываем выбор ДО первых обращений к базе: второй быстрый тап увидит устаревшее окно
        # и не создаст рассылки повторно. Если применить не получится — выбор вернём.
        await state.clear()
        if not await _apply(callback, data, bool(callback_data.v), app, callback_answer):
            await state.set_state(Picker.picking)
            await state.set_data(data)
        return
    if action == "t":
        selected ^= {callback_data.v}
    elif action == "pg":
        data["page"] = callback_data.v
    elif action == "all":
        selected = set(await _eligible_chat_ids(app))
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
    app: App,
    callback_answer: CallbackAnswer,
) -> bool:
    """Применяет выбор. False — применить не вышло и окно выбора стоит оставить открытым."""
    source = await app.repo.get_campaign(int(data["src"]))
    if source is None or not source.is_draft:
        callback_answer.text = "Черновик уже удалён"
        await show(app, callback, await screens.main_menu(app))
        return True
    chats = {c.id: c for c in await app.repo.list_chats(statuses=("active",))}
    selected = [chat_id for chat_id in data.get("sel", []) if chat_id in chats]
    if not selected:
        callback_answer.text = "Отметьте хотя бы один чат"
        callback_answer.show_alert = True
        return False
    if activate:
        posts = await app.repo.list_posts(source.id)
        now = app.scheduler.now() if app.scheduler else 0
        problems = schedule_problems(source, posts, app.settings.tz, now)
        if not any(chats[chat_id].can_post for chat_id in selected):
            problems.append("Ни в одном из отмеченных чатов у бота нет права публиковать")
        if problems:
            callback_answer.text = problems[0] + " — или примените без запуска"
            callback_answer.show_alert = True
            return False

    result = await app.repo.apply_draft(source.id, selected, activate=activate)
    if result is None:
        callback_answer.text = "Черновик уже удалён"
        await show(app, callback, await screens.main_menu(app))
        return True
    warnings: list[str] = []
    no_post = [chats[chat_id].title for chat_id in selected if not chats[chat_id].can_post]
    if no_post:
        warnings.append(f"⚠️ Нет права публиковать — туда посты не пойдут, пока его нет: {t.names_label(no_post, 10)}")
    no_pin = [chats[chat_id].title for chat_id in selected if not chats[chat_id].can_pin]
    if source.pin and no_pin:
        warnings.append(f"📌 Нет права закреплять: {t.names_label(no_pin, 10)}")
    if app.scheduler:
        await app.scheduler.reschedule(result.touched)
    await show(app, callback, await screens.apply_summary(app, source.id, result, warnings))
    return True
