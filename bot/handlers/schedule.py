"""Расписание: время, «N раз в день», дни недели, разброс, период."""

from __future__ import annotations

from datetime import date, datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.services import schedule_utils as su
from bot.states import Input
from bot.ui import screens
from bot.ui import texts as t
from bot.ui.callbacks import Nav, SchedAct
from bot.ui.keyboards import btn
from bot.ui.render import finish_input, prompt, show

router = Router(name="schedule")
router.message.filter(F.chat.type == "private")

WEEKDAY_PRESETS = {0: [0, 1, 2, 3, 4, 5, 6], 1: [0, 1, 2, 3, 4], 2: [5, 6]}


async def _gone(callback: CallbackQuery, app: App, callback_answer: CallbackAnswer) -> None:
    callback_answer.text = "Это уже удалено"
    await show(app, callback, await screens.main_menu(app))


def _today(app: App) -> date:
    now = app.scheduler.now() if app.scheduler else int(datetime.now().timestamp())
    return datetime.fromtimestamp(now, app.settings.tz).date()


async def _save(app: App, campaign_id: int, **values: object) -> None:
    await app.repo.update_campaign(campaign_id, **values)
    if app.scheduler:
        await app.scheduler.reschedule([campaign_id])


async def _render(app: App, event: Message | CallbackQuery, campaign_id: int, note: str | None = None) -> None:
    screen = await screens.schedule_view(app, campaign_id, note=note)
    await show(app, event, screen or await screens.main_menu(app))


# ------------------------------------------------------------------ точное время


@router.callback_query(SchedAct.filter(F.a == "times"))
async def ask_times(
    callback: CallbackQuery,
    callback_data: SchedAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    campaign = await app.repo.get_campaign(callback_data.id)
    if campaign is None:
        return await _gone(callback, app, callback_answer)
    text = (
        "🕐 Пришлите время публикаций через пробел или запятую.\n"
        "Пример: <code>09:00 13:30 18:00</code>\n\n"
        f"Сейчас: {t.times_label(campaign.times)}"
    )
    await prompt(app, callback, state, Input.times, text, cancel=Nav(to="sched", id=campaign.id), camp_id=campaign.id)


@router.message(Input.times, F.text)
async def on_times(message: Message, state: FSMContext, app: App) -> None:
    campaign_id = int((await state.get_data())["camp_id"])
    try:
        times = su.parse_times(message.text)
    except su.ScheduleError as error:
        await message.reply(f"⚠️ {t.esc(error)}")
        return
    await _save(app, campaign_id, times=times)
    await finish_input(app, message, state)
    await _render(app, message, campaign_id, note=f"✅ Время: {', '.join(times)}")


# ------------------------------------------------------------------ N раз в день


@router.callback_query(SchedAct.filter(F.a == "count"))
async def choose_count(
    callback: CallbackQuery,
    callback_data: SchedAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    await state.clear()
    screen = await screens.count_picker(app, callback_data.id)
    if screen is None:
        return await _gone(callback, app, callback_answer)
    await show(app, callback, screen)


@router.callback_query(SchedAct.filter(F.a == "n"))
async def choose_window(
    callback: CallbackQuery, callback_data: SchedAct, app: App, callback_answer: CallbackAnswer
) -> None:
    screen = await screens.window_picker(app, callback_data.id, callback_data.v)
    if screen is None:
        return await _gone(callback, app, callback_answer)
    await show(app, callback, screen)


@router.callback_query(SchedAct.filter(F.a == "ncustom"))
async def ask_count(callback: CallbackQuery, callback_data: SchedAct, state: FSMContext, app: App) -> None:
    await prompt(
        app,
        callback,
        state,
        Input.count,
        f"🔢 Сколько раз в день публиковать? Пришлите число от 1 до {su.MAX_TIMES}.",
        cancel=Nav(to="sched", id=callback_data.id),
        camp_id=callback_data.id,
    )


@router.message(Input.count, F.text)
async def on_count(message: Message, state: FSMContext, app: App) -> None:
    campaign_id = int((await state.get_data())["camp_id"])
    text = message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= su.MAX_TIMES:
        await message.reply(f"⚠️ Нужно число от 1 до {su.MAX_TIMES}.")
        return
    await finish_input(app, message, state)
    screen = await screens.window_picker(app, campaign_id, int(text))
    await show(app, message, screen or await screens.main_menu(app))


@router.callback_query(SchedAct.filter(F.a == "w"))
async def apply_window(
    callback: CallbackQuery, callback_data: SchedAct, app: App, callback_answer: CallbackAnswer
) -> None:
    if not 0 <= callback_data.w < len(screens.WINDOWS):
        return
    try:
        times = su.spread_times(callback_data.v, *screens.WINDOWS[callback_data.w])
    except su.ScheduleError as error:
        callback_answer.text = str(error)
        callback_answer.show_alert = True
        return
    if await app.repo.get_campaign(callback_data.id) is None:
        return await _gone(callback, app, callback_answer)
    await _save(app, callback_data.id, times=times)
    callback_answer.text = "✅ Расписание обновлено"
    await _render(app, callback, callback_data.id, note=f"✅ Время: {', '.join(times)}")


@router.callback_query(SchedAct.filter(F.a == "wcustom"))
async def ask_window(callback: CallbackQuery, callback_data: SchedAct, state: FSMContext, app: App) -> None:
    await prompt(
        app,
        callback,
        state,
        Input.window,
        f"🕘 В какие часы публиковать {callback_data.v} {t.plural(callback_data.v, 'раз', 'раза', 'раз')} в день?\n"
        "Пришлите начало и конец, например <code>09:00-21:00</code>, или слово <code>круглосуточно</code>.",
        cancel=Nav(to="sched", id=callback_data.id),
        camp_id=callback_data.id,
        count=callback_data.v,
    )


@router.message(Input.window, F.text)
async def on_window(message: Message, state: FSMContext, app: App) -> None:
    data = await state.get_data()
    campaign_id, count = int(data["camp_id"]), int(data["count"])
    try:
        times = su.spread_times(count, *su.parse_window(message.text))
    except su.ScheduleError as error:
        await message.reply(f"⚠️ {t.esc(error)}")
        return
    await _save(app, campaign_id, times=times)
    await finish_input(app, message, state)
    await _render(app, message, campaign_id, note=f"✅ Время: {', '.join(times)}")


# ---------------------------------------------------------------- дни и разброс


@router.callback_query(SchedAct.filter(F.a == "wd"))
async def toggle_weekday(
    callback: CallbackQuery, callback_data: SchedAct, app: App, callback_answer: CallbackAnswer
) -> None:
    campaign = await app.repo.get_campaign(callback_data.id)
    if campaign is None or not 0 <= callback_data.v <= 6:
        return await _gone(callback, app, callback_answer)
    days = set(campaign.weekdays or [])
    days ^= {callback_data.v}
    await _save(app, campaign.id, weekdays=sorted(days))
    if not days:
        callback_answer.text = "Не выбрано ни одного дня — публикаций не будет"
    await _render(app, callback, campaign.id)


@router.callback_query(SchedAct.filter(F.a == "wds"))
async def set_weekdays(
    callback: CallbackQuery, callback_data: SchedAct, app: App, callback_answer: CallbackAnswer
) -> None:
    if await app.repo.get_campaign(callback_data.id) is None:
        return await _gone(callback, app, callback_answer)
    await _save(app, callback_data.id, weekdays=list(WEEKDAY_PRESETS.get(callback_data.v, WEEKDAY_PRESETS[0])))
    await _render(app, callback, callback_data.id)


@router.callback_query(SchedAct.filter(F.a == "jit"))
async def cycle_jitter(
    callback: CallbackQuery, callback_data: SchedAct, app: App, callback_answer: CallbackAnswer
) -> None:
    campaign = await app.repo.get_campaign(callback_data.id)
    if campaign is None:
        return await _gone(callback, app, callback_answer)
    steps = su.JITTER_STEPS
    current = campaign.jitter_min if campaign.jitter_min in steps else 0
    value = steps[(steps.index(current) + 1) % len(steps)]
    await _save(app, campaign.id, jitter_min=value)
    callback_answer.text = f"Разброс: ±{value} мин" if value else "Разброс выключен"
    await _render(app, callback, campaign.id)


# --------------------------------------------------------------------- период


@router.callback_query(SchedAct.filter(F.a.in_({"start", "end"})))
async def ask_date(
    callback: CallbackQuery,
    callback_data: SchedAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    campaign = await app.repo.get_campaign(callback_data.id)
    if campaign is None:
        return await _gone(callback, app, callback_answer)
    is_start = callback_data.a == "start"
    current = campaign.start_date if is_start else campaign.end_date
    what = "начала" if is_start else "окончания"
    text = (
        f"📆 Пришлите дату {what} в формате <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>.\n"
        + (
            "С этого дня рассылка начнёт публиковать."
            if is_start
            else "Этот день — последний: после него рассылка выключится сама."
        )
        + (f"\n\nСейчас: {t.fmt_date(current)}" if current else "")
    )
    extra = []
    if current:
        extra.append([btn(f"✖️ Убрать дату {what}", SchedAct(a="clr" + callback_data.a, id=campaign.id))])
    await prompt(
        app,
        callback,
        state,
        Input.start_date if is_start else Input.end_date,
        text,
        cancel=Nav(to="sched", id=campaign.id),
        extra=extra,
        camp_id=campaign.id,
    )


@router.callback_query(SchedAct.filter(F.a.in_({"clrstart", "clrend"})))
async def clear_date(
    callback: CallbackQuery,
    callback_data: SchedAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    await state.clear()
    if await app.repo.get_campaign(callback_data.id) is None:
        return await _gone(callback, app, callback_answer)
    field = "start_date" if callback_data.a == "clrstart" else "end_date"
    await _save(app, callback_data.id, **{field: None})
    await _render(app, callback, callback_data.id, note="✅ Дата убрана")


@router.message(Input.start_date, F.text)
async def on_start_date(message: Message, state: FSMContext, app: App) -> None:
    campaign_id = int((await state.get_data())["camp_id"])
    campaign = await app.repo.get_campaign(campaign_id)
    try:
        value = su.parse_date(message.text, _today(app))
    except su.ScheduleError as error:
        await message.reply(f"⚠️ {t.esc(error)}")
        return
    if campaign and campaign.end_date and value > date.fromisoformat(campaign.end_date):
        await message.reply("⚠️ Дата начала позже даты окончания.")
        return
    await _save(app, campaign_id, start_date=value.isoformat())
    await finish_input(app, message, state)
    await _render(app, message, campaign_id, note=f"✅ Начало: {value:%d.%m.%Y}")


@router.message(Input.end_date, F.text)
async def on_end_date(message: Message, state: FSMContext, app: App) -> None:
    campaign_id = int((await state.get_data())["camp_id"])
    campaign = await app.repo.get_campaign(campaign_id)
    try:
        value = su.parse_date(message.text, _today(app))
    except su.ScheduleError as error:
        await message.reply(f"⚠️ {t.esc(error)}")
        return
    if value < _today(app):
        await message.reply("⚠️ Эта дата уже прошла.")
        return
    if campaign and campaign.start_date and value < date.fromisoformat(campaign.start_date):
        await message.reply("⚠️ Дата окончания раньше даты начала.")
        return
    await _save(app, campaign_id, end_date=value.isoformat())
    await finish_input(app, message, state)
    await _render(app, message, campaign_id, note=f"✅ Последний день: {value:%d.%m.%Y}")
