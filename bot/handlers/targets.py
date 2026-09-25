"""Чаты рассылки: галочки, «Все/Снять все», тема форума в каждом чате, снятие паузы после ошибок.

Состояния нет: каждое нажатие сразу сохраняется, а нужное значение приходит в самой кнопке,
поэтому старые кнопки не могут ничего «переключить наоборот».
"""

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
from bot.ui.callbacks import Nav, TargetAct
from bot.ui.keyboards import btn
from bot.ui.render import finish_input, input_value, prompt, show

router = Router(name="targets")
router.message.filter(F.chat.type == "private")

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


async def _render(
    app: App, event: Message | CallbackQuery, campaign_id: int, page: int = 0, note: str | None = None
) -> None:
    screen = await screens.targets_view(app, campaign_id, page, note)
    await show(app, event, screen or await screens.campaigns_list(app))


@router.callback_query(TargetAct.filter(F.a.in_({"on", "off"})))
async def toggle_chat(
    callback: CallbackQuery, callback_data: TargetAct, app: App, callback_answer: CallbackAnswer
) -> None:
    campaign = await app.repo.get_campaign(callback_data.id)
    if campaign is None or campaign.is_draft:
        callback_answer.text = "Рассылка уже удалена"
        await show(app, callback, await screens.campaigns_list(app))
        return
    on = callback_data.a == "on"
    chat = await app.repo.get_chat(callback_data.v)
    if chat is None:
        callback_answer.text = "Чат уже удалён"
    elif on and chat.status != "active":
        callback_answer.text = "Бот сейчас не может публиковать в этом чате"
        callback_answer.show_alert = True
    else:
        await app.repo.set_target(campaign.id, chat.id, on)
        if on and not chat.can_post:
            callback_answer.text = "⚠️ У бота нет права публиковать в этом чате — посты туда не пойдут, пока его нет"
            callback_answer.show_alert = True
        elif on and campaign.pin and not chat.can_pin:
            callback_answer.text = "⚠️ Здесь у бота нет права закреплять сообщения"
    await _render(app, callback, campaign.id, callback_data.p)


@router.callback_query(TargetAct.filter(F.a == "pg"))
async def change_page(callback: CallbackQuery, callback_data: TargetAct, app: App) -> None:
    await _render(app, callback, callback_data.id, callback_data.v)


@router.callback_query(TargetAct.filter(F.a == "all"))
async def select_all(
    callback: CallbackQuery, callback_data: TargetAct, app: App, callback_answer: CallbackAnswer
) -> None:
    chats = await app.repo.list_chats(statuses=("active",))
    added = await app.repo.add_targets(callback_data.id, [c.id for c in chats])
    callback_answer.text = f"☑️ Добавлено чатов: {added}" if added else "Все чаты уже отмечены"
    await _render(app, callback, callback_data.id, callback_data.p)


@router.callback_query(TargetAct.filter(F.a == "none"))
async def select_none(
    callback: CallbackQuery, callback_data: TargetAct, app: App, callback_answer: CallbackAnswer
) -> None:
    removed = await app.repo.clear_targets(callback_data.id)
    callback_answer.text = f"⬜ Снято чатов: {removed}" if removed else "Чаты и так не отмечены"
    await _render(app, callback, callback_data.id, callback_data.p)


@router.callback_query(TargetAct.filter(F.a == "resume"))
async def resume_chat(
    callback: CallbackQuery, callback_data: TargetAct, app: App, callback_answer: CallbackAnswer
) -> None:
    link = await app.repo.resume_target(callback_data.id, callback_data.v)
    callback_answer.text = "▶️ Публикации в этот чат возобновлены" if link else "Чат уже не отмечен в рассылке"
    await _render(app, callback, callback_data.id, callback_data.p)


# ---------------------------------------------------------------------- тема форума


@router.callback_query(TargetAct.filter(F.a == "thr"))
async def ask_thread(
    callback: CallbackQuery,
    callback_data: TargetAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    targets = await app.repo.campaign_targets(callback_data.id)
    target = next((x for x in targets if x.chat.id == callback_data.v), None)
    if target is None:
        callback_answer.text = "Чат уже не отмечен в рассылке"
        await _render(app, callback, callback_data.id, callback_data.p)
        return
    current = f"#{target.link.thread_id}" if target.link.thread_id else "General"
    text = (
        f"🧵 В какую тему форума «{t.esc(target.chat.title)}» публиковать?\n\n"
        "Откройте нужную тему → ⋯ → «Копировать ссылку» и пришлите ссылку сюда "
        "(вида <code>https://t.me/c/1234567890/45</code>). Можно прислать просто номер темы.\n\n"
        f"Сейчас: {current}"
    )
    await prompt(
        app,
        callback,
        state,
        Input.thread,
        text,
        cancel=Nav(to="tgt", id=callback_data.id, page=callback_data.p),
        extra=[
            [
                btn(
                    "General (по умолчанию)",
                    TargetAct(a="clrthr", id=callback_data.id, v=callback_data.v, p=callback_data.p),
                )
            ]
        ],
        camp_id=callback_data.id,
        chat_id=callback_data.v,
        page=callback_data.p,
    )


@router.message(Input.thread, F.text)
async def on_thread(message: Message, state: FSMContext, app: App) -> None:
    campaign_id = await input_value(state, "camp_id")
    chat_id = await input_value(state, "chat_id")
    page = await input_value(state, "page") or 0
    if campaign_id is None or chat_id is None:
        await state.clear()
        return
    try:
        topic = parse_topic(message.text)
    except ValueError as error:
        await message.reply(f"⚠️ {t.esc(error)}")
        return
    link = await app.repo.update_target(campaign_id, chat_id, thread_id=topic)
    await finish_input(app, message, state)
    topic_text = f"#{topic}" if topic else "General"
    note = f"✅ Тема: {topic_text}" if link else "⚠️ Чат уже не отмечен в рассылке"
    await _render(app, message, campaign_id, page, note)


@router.callback_query(TargetAct.filter(F.a == "clrthr"))
async def clear_thread(callback: CallbackQuery, callback_data: TargetAct, app: App) -> None:
    link = await app.repo.update_target(callback_data.id, callback_data.v, thread_id=None)
    note = "✅ Тема: General" if link else "⚠️ Чат уже не отмечен в рассылке"
    await _render(app, callback, callback_data.id, callback_data.p, note)
