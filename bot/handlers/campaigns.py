"""Экран рассылки/черновика: создание, запуск, остановка, предпросмотр, отправка, черновики."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.db.repo import DRAFT
from bot.services.campaigns import readiness_problems
from bot.services.preview import send_preview
from bot.states import Input, Picker
from bot.ui import screens
from bot.ui import texts as t
from bot.ui.callbacks import CampAct, Nav
from bot.ui.render import finish_input, input_value, prompt, show

router = Router(name="campaigns")
router.message.filter(F.chat.type == "private")

NEW_CAMPAIGN_NOTE = (
    "✨ Рассылка «{name}» создана. Отметьте чаты, в которые публиковать, затем добавьте посты («📝 Посты»), "
    "задайте время («⏰ Расписание») и нажмите «▶️ Запустить»."
)


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
    if campaign is None or campaign.is_draft:
        return await _gone(callback, app, callback_answer)
    posts = await app.repo.list_posts(campaign.id)
    targets = await app.repo.campaign_targets(campaign.id)
    problems = readiness_problems(campaign, posts, targets, app.settings.tz, _now(app))
    if problems:
        callback_answer.text = problems[0]
        callback_answer.show_alert = True
        return
    await app.repo.update_campaign(campaign.id, is_active=True, last_error=None)
    if app.scheduler:
        await app.scheduler.reschedule([campaign.id])
    fresh = await app.repo.get_campaign(campaign.id)
    if fresh and fresh.next_run_ts:
        callback_answer.text = "▶️ Запущено. Ближайшая: " + t.fmt_ts(fresh.next_run_ts, app.settings.tz, _now(app))
    else:
        callback_answer.text = "▶️ Запущено"
    await show(app, callback, await screens.campaign_view(app, campaign.id, user_id=callback.from_user.id))


@router.callback_query(CampAct.filter(F.a == "off"))
async def stop_campaign(
    callback: CallbackQuery, callback_data: CampAct, app: App, callback_answer: CallbackAnswer
) -> None:
    campaign = await app.repo.update_campaign(callback_data.id, is_active=False, next_slot_ts=None, next_run_ts=None)
    if campaign is None:
        return await _gone(callback, app, callback_answer)
    callback_answer.text = "⏸ Рассылка остановлена"
    await show(app, callback, await screens.campaign_view(app, campaign.id, user_id=callback.from_user.id))


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
    screen = await screens.campaign_view(
        app, callback_data.id, note="\n".join(filter(None, [header, note])), user_id=callback.from_user.id
    )
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
    screen = await screens.campaign_view(app, callback_data.id, note=note, user_id=callback.from_user.id)
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


@router.callback_query(CampAct.filter(F.a == "apply"))
async def apply_to_chats(
    callback: CallbackQuery,
    callback_data: CampAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    data = {"mode": "draft", "src": callback_data.id, "sel": [], "page": 0}
    screen = await screens.picker_view(app, data)
    if screen is None:
        return await _gone(callback, app, callback_answer)
    await state.set_state(Picker.picking)
    await state.set_data(data)
    await show(app, callback, screen)


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
    updated = await app.repo.sync_draft(callback_data.id)
    if app.scheduler and updated:
        await app.scheduler.reschedule([c.id for c in updated])
    callback_answer.text = f"🔄 Обновлено рассылок: {len(updated)}"
    screen = await screens.campaign_view(app, callback_data.id, note=f"🔄 Обновлено рассылок: {len(updated)}")
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
    campaign_id = await input_value(state, "camp_id")
    if campaign_id is None:
        await state.clear()
        return
    name = " ".join(message.text.split())
    if not name:
        await message.reply("Название не может быть пустым.")
        return
    campaign = await app.repo.update_campaign(campaign_id, name=name)
    await finish_input(app, message, state)
    if campaign is None:
        await show(app, message, await screens.main_menu(app))
        return
    screen = await screens.campaign_view(app, campaign.id, note="✏️ Название обновлено", user_id=message.from_user.id)
    await show(app, message, screen or await screens.main_menu(app))


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
    origin = app.origin(callback.from_user.id, campaign.id)
    await app.repo.delete_campaign(campaign.id)
    app.remember_origin(callback.from_user.id, campaign.id, -1)
    callback_answer.text = "🗑 Удалено"
    if campaign.is_draft:
        await show(app, callback, await screens.drafts_list(app))
        return
    screen = await screens.chat_view(app, origin) if origin else None
    await show(app, callback, screen or await screens.campaigns_list(app))


@router.callback_query(CampAct.filter(F.a == "newdraft"))
async def new_draft(callback: CallbackQuery, app: App) -> None:
    number = len(await app.repo.list_drafts()) + 1
    draft = await app.repo.create_campaign(f"Черновик {number}", kind=DRAFT)
    note = "👉 Добавьте посты, задайте расписание и опции — потом примените черновик к чатам."
    await show(app, callback, await screens.campaign_view(app, draft.id, note=note))


@router.callback_query(CampAct.filter(F.a == "new"))
async def new_campaign(callback: CallbackQuery, app: App) -> None:
    """Новая рассылка из списка «📬 Рассылки»: сначала — выбор чатов."""
    number = await app.repo.count_campaigns() + 1
    campaign = await app.repo.create_campaign(f"Рассылка {number}")
    app.remember_origin(callback.from_user.id, campaign.id, -1)
    note = NEW_CAMPAIGN_NOTE.format(name=t.esc(campaign.name))
    await show(app, callback, await screens.targets_view(app, campaign.id, note=note))
