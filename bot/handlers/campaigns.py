"""Экран рассылки/черновика: запуск, остановка, предпросмотр, отправка, копирование."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.services.campaigns import readiness_problems
from bot.services.preview import send_preview
from bot.states import Input, Picker
from bot.ui import screens
from bot.ui import texts as t
from bot.ui.callbacks import CampAct, Nav
from bot.ui.render import finish_input, prompt, show

router = Router(name="campaigns")
router.message.filter(F.chat.type == "private")


async def _gone(callback: CallbackQuery, app: App, callback_answer: CallbackAnswer) -> None:
    callback_answer.text = "Это уже удалено"
    await show(app, callback, await screens.main_menu(app))


def _now(app: App) -> int:
    return app.scheduler.now() if app.scheduler else 0


@router.callback_query(CampAct.filter(F.a == "on"))
async def start_campaign(
    callback: CallbackQuery, callback_data: CampAct, app: App, callback_answer: CallbackAnswer
) -> None:
    campaign = await app.repo.get_campaign(callback_data.id)
    if campaign is None or campaign.chat_id is None:
        return await _gone(callback, app, callback_answer)
    posts = await app.repo.list_posts(campaign.id)
    chat = await app.repo.get_chat(campaign.chat_id)
    problems = readiness_problems(campaign, posts, chat, app.settings.tz, _now(app))
    if problems:
        callback_answer.text = problems[0]
        callback_answer.show_alert = True
        return
    await app.repo.update_campaign(campaign.id, is_active=True, fail_count=0)
    if app.scheduler:
        await app.scheduler.reschedule([campaign.id])
    fresh = await app.repo.get_campaign(campaign.id)
    if fresh and fresh.next_run_ts:
        callback_answer.text = "▶️ Запущено. Ближайшая: " + t.fmt_ts(fresh.next_run_ts, app.settings.tz, _now(app))
    else:
        callback_answer.text = "▶️ Запущено"
    await show(app, callback, await screens.campaign_view(app, campaign.id))


@router.callback_query(CampAct.filter(F.a == "off"))
async def stop_campaign(
    callback: CallbackQuery, callback_data: CampAct, app: App, callback_answer: CallbackAnswer
) -> None:
    campaign = await app.repo.update_campaign(callback_data.id, is_active=False, next_slot_ts=None, next_run_ts=None)
    if campaign is None:
        return await _gone(callback, app, callback_answer)
    callback_answer.text = "⏸ Рассылка остановлена"
    await show(app, callback, await screens.campaign_view(app, campaign.id))


@router.callback_query(CampAct.filter(F.a == "preview"))
async def preview(callback: CallbackQuery, callback_data: CampAct, app: App, callback_answer: CallbackAnswer) -> None:
    posts = await app.repo.list_posts(callback_data.id)
    if not posts:
        callback_answer.text = "В рассылке пока нет постов"
        callback_answer.show_alert = True
        return
    callback_answer.disabled = True
    await callback.answer("Отправляю предпросмотр…")
    note = await send_preview(app, posts, callback.from_user.id)
    header = f"👆 Так посты будут выглядеть в чате ({len(posts)})."
    screen = await screens.campaign_view(app, callback_data.id, note="\n".join(filter(None, [header, note])))
    if screen:
        await show(app, callback, screen, new=True)


@router.callback_query(CampAct.filter(F.a == "send"))
async def confirm_send(
    callback: CallbackQuery, callback_data: CampAct, app: App, callback_answer: CallbackAnswer
) -> None:
    screen = await screens.send_now_confirm(app, callback_data.id)
    if screen is None:
        return await _gone(callback, app, callback_answer)
    await show(app, callback, screen)


@router.callback_query(CampAct.filter(F.a == "send_ok"))
async def send_now(callback: CallbackQuery, callback_data: CampAct, app: App, callback_answer: CallbackAnswer) -> None:
    if app.scheduler is None:
        return
    callback_answer.disabled = True
    await callback.answer("Отправляю…")
    ok, message = await app.scheduler.send_now(callback_data.id)
    note = ("✅ " if ok else "❌ Не отправлено: ") + t.esc(message)
    screen = await screens.campaign_view(app, callback_data.id, note=note)
    if screen:
        await show(app, callback, screen)


@router.callback_query(CampAct.filter(F.a == "todraft"))
async def save_as_draft(
    callback: CallbackQuery, callback_data: CampAct, app: App, callback_answer: CallbackAnswer
) -> None:
    draft = await app.repo.save_as_draft(callback_data.id)
    if draft is None:
        return await _gone(callback, app, callback_answer)
    screen = await screens.draft_saved(app, draft.id, callback_data.id)
    if screen:
        await show(app, callback, screen)


async def _open_picker(
    callback: CallbackQuery,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
    mode: str,
    source_id: int,
) -> None:
    data = {"mode": mode, "src": source_id, "sel": [], "page": 0}
    screen = await screens.picker_view(app, data)
    if screen is None:
        return await _gone(callback, app, callback_answer)
    await state.set_state(Picker.picking)
    await state.set_data(data)
    await show(app, callback, screen)


@router.callback_query(CampAct.filter(F.a == "copy"))
async def copy_to_chats(
    callback: CallbackQuery,
    callback_data: CampAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    await _open_picker(callback, state, app, callback_answer, "copy", callback_data.id)


@router.callback_query(CampAct.filter(F.a == "apply"))
async def apply_to_chats(
    callback: CallbackQuery,
    callback_data: CampAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    await _open_picker(callback, state, app, callback_answer, "draft", callback_data.id)


@router.callback_query(CampAct.filter(F.a == "sync"))
async def confirm_sync(
    callback: CallbackQuery, callback_data: CampAct, app: App, callback_answer: CallbackAnswer
) -> None:
    screen = await screens.sync_confirm(app, callback_data.id)
    if screen is None:
        return await _gone(callback, app, callback_answer)
    await show(app, callback, screen)


@router.callback_query(CampAct.filter(F.a == "sync_ok"))
async def sync_draft(
    callback: CallbackQuery, callback_data: CampAct, app: App, callback_answer: CallbackAnswer
) -> None:
    linked = await app.repo.linked_campaigns(callback_data.id)
    chat_ids = sorted({c.chat_id for c in linked if c.chat_id is not None})
    results = await app.repo.apply_template(callback_data.id, chat_ids, activate=None, link=True)
    if app.scheduler and results:
        await app.scheduler.reschedule([c.id for c, _ in results])
    callback_answer.text = f"🔄 Обновлено рассылок: {len(results)}"
    screen = await screens.campaign_view(app, callback_data.id, note=f"🔄 Обновлено рассылок: {len(results)}")
    if screen is None:
        return await _gone(callback, app, callback_answer)
    await show(app, callback, screen)


@router.callback_query(CampAct.filter(F.a == "rename"))
async def ask_rename(
    callback: CallbackQuery,
    callback_data: CampAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    campaign = await app.repo.get_campaign(callback_data.id)
    if campaign is None:
        return await _gone(callback, app, callback_answer)
    await prompt(
        app,
        callback,
        state,
        Input.rename,
        f"✏️ Пришлите новое название (до 64 символов).\n\nСейчас: «{t.esc(campaign.name)}»",
        cancel=Nav(to="camp", id=campaign.id),
        camp_id=campaign.id,
    )


@router.message(Input.rename, F.text)
async def on_rename(message: Message, state: FSMContext, app: App) -> None:
    data = await state.get_data()
    name = " ".join(message.text.split())
    if not name:
        await message.reply("Название не может быть пустым.")
        return
    campaign = await app.repo.update_campaign(int(data["camp_id"]), name=name)
    await finish_input(app, message, state)
    if campaign is None:
        await show(app, message, await screens.main_menu(app))
        return
    await show(app, message, await screens.campaign_view(app, campaign.id, note="✏️ Название обновлено"))


@router.callback_query(CampAct.filter(F.a == "del"))
async def confirm_delete(
    callback: CallbackQuery, callback_data: CampAct, app: App, callback_answer: CallbackAnswer
) -> None:
    screen = await screens.campaign_delete_confirm(app, callback_data.id)
    if screen is None:
        return await _gone(callback, app, callback_answer)
    await show(app, callback, screen)


@router.callback_query(CampAct.filter(F.a == "del_ok"))
async def delete_campaign(
    callback: CallbackQuery, callback_data: CampAct, app: App, callback_answer: CallbackAnswer
) -> None:
    campaign = await app.repo.get_campaign(callback_data.id)
    if campaign is None:
        return await _gone(callback, app, callback_answer)
    await app.repo.delete_campaign(campaign.id)
    callback_answer.text = "🗑 Удалено"
    if campaign.chat_id is not None:
        screen = await screens.chat_view(app, campaign.chat_id)
        await show(app, callback, screen or await screens.chats_list(app))
    else:
        await show(app, callback, await screens.drafts_list(app))


@router.callback_query(CampAct.filter(F.a == "newdraft"))
async def new_draft(callback: CallbackQuery, app: App) -> None:
    number = len(await app.repo.list_drafts()) + 1
    draft = await app.repo.create_campaign(None, f"Черновик {number}")
    note = "👉 Добавьте посты, задайте расписание и опции — потом примените черновик к чатам."
    await show(app, callback, await screens.campaign_view(app, draft.id, note=note))
