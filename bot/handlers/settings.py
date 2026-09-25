"""Настройки: часовой пояс, пауза всех рассылок, уведомления, резервная копия."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.states import Input
from bot.ui import screens
from bot.ui import texts as t
from bot.ui.callbacks import Nav, SetAct
from bot.ui.render import finish_input, prompt, show

router = Router(name="settings")
router.message.filter(F.chat.type == "private")

_OFFSET_RE = re.compile(r"^(?:utc|gmt)?\s*([+-−])\s*(\d{1,2})$", re.IGNORECASE)


def parse_timezone(text: str) -> str:
    """IANA-имя (Europe/Berlin) или смещение (UTC+3, +5)."""
    value = text.strip()
    match = _OFFSET_RE.match(value.replace(" ", ""))
    if match:
        sign, hours = match.group(1), int(match.group(2))
        if hours > 14:
            raise ValueError("Такого смещения не бывает")
        if hours == 0:
            return "UTC"
        # В базе IANA у зон Etc/GMT знак перевёрнут: UTC+3 — это Etc/GMT-3
        return f"Etc/GMT{'-' if sign == '+' else '+'}{hours}"
    normalized = value.replace(" ", "_")
    for name in dict.fromkeys([normalized, normalized.title()]):  # «europe/berlin» -> «Europe/Berlin»
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError):
            continue
    raise ValueError("Не знаю такого часового пояса. Пример: Europe/Berlin, Asia/Dubai или UTC+3")


async def _set_timezone(app: App, name: str) -> None:
    await app.settings.set(app.repo, "timezone", name)
    if app.scheduler:
        await app.scheduler.reschedule()


@router.callback_query(SetAct.filter(F.a == "pause"))
async def toggle_pause(callback: CallbackQuery, app: App, callback_answer: CallbackAnswer) -> None:
    paused = not app.settings.paused_all
    await app.settings.set(app.repo, "paused_all", paused)
    if not paused and app.scheduler:
        # Отсчёт от текущего момента: пропущенные за паузу слоты не публикуются пачкой
        await app.scheduler.reschedule()
    callback_answer.text = "⏸ Все рассылки на паузе" if paused else "▶️ Рассылки возобновлены"
    await show(app, callback, await screens.settings_view(app))


@router.callback_query(SetAct.filter(F.a == "notify"))
async def toggle_notify(callback: CallbackQuery, app: App) -> None:
    await app.settings.set(app.repo, "notify_errors", not app.settings.notify_errors)
    await show(app, callback, await screens.settings_view(app))


@router.callback_query(SetAct.filter(F.a == "tz"))
async def choose_timezone(
    callback: CallbackQuery, callback_data: SetAct, app: App, callback_answer: CallbackAnswer
) -> None:
    if not 0 <= callback_data.v < len(screens.TIMEZONES):
        return
    city, name = screens.TIMEZONES[callback_data.v]
    await _set_timezone(app, name)
    callback_answer.text = f"🌍 Часовой пояс: {city}"
    await show(app, callback, await screens.settings_view(app, note=f"🌍 Часовой пояс изменён: {city} ({name})"))


@router.callback_query(SetAct.filter(F.a == "tzin"))
async def ask_timezone(callback: CallbackQuery, state: FSMContext, app: App) -> None:
    await prompt(
        app,
        callback,
        state,
        Input.timezone,
        "🌍 Пришлите часовой пояс в формате IANA (например, <code>Europe/Berlin</code>, <code>Asia/Dubai</code>) "
        "или смещение от UTC (например, <code>UTC+3</code>).",
        cancel=Nav(to="tz"),
    )


@router.message(Input.timezone, F.text)
async def on_timezone(message: Message, state: FSMContext, app: App) -> None:
    try:
        name = parse_timezone(message.text)
    except ValueError as error:
        await message.reply(f"⚠️ {t.esc(error)}")
        return
    await _set_timezone(app, name)
    await finish_input(app, message, state)
    now = app.scheduler.now() if app.scheduler else int(datetime.now().timestamp())
    note = f"🌍 Часовой пояс изменён: {t.tz_label(name, app.settings.tz, now)}"
    await show(app, message, await screens.settings_view(app, note=note))


@router.callback_query(SetAct.filter(F.a == "backup"))
async def backup(callback: CallbackQuery, app: App, callback_answer: CallbackAnswer) -> None:
    callback_answer.disabled = True
    await callback.answer("Готовлю резервную копию…")
    stamp = datetime.now(app.settings.tz).strftime("%Y-%m-%d_%H-%M")
    target = app.db.path.with_name(f"backup-{stamp}.db")
    try:
        await app.db.backup_to(target)
        await app.bot.send_document(
            callback.from_user.id,
            FSInputFile(target, filename=f"autopost-backup-{stamp}.db"),
            caption=(
                "💾 Резервная копия базы: чаты, рассылки, посты и черновики.\n\n"
                "Чтобы восстановить: остановите бота, замените файл базы (по умолчанию "
                "<code>data/autopost.db</code>) этим файлом и запустите бота снова."
            ),
        )
    finally:
        target.unlink(missing_ok=True)
