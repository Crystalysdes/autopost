"""Настройка защиты чатов: антиспам, обязательная подписка, автоприём заявок и скрытые админы
(экраны — bot/ui/guard.py)."""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.services import spam, trusted
from bot.states import Input
from bot.ui import guard, screens
from bot.ui import texts as t
from bot.ui.callbacks import Guard
from bot.ui.keyboards import btn
from bot.ui.render import finish_input, prompt, show

router = Router(name="protection")
router.message.filter(F.chat.type == "private")

MAX_WORDS = 300
MAX_WORD_LENGTH = 60
_ALLOW_RE = re.compile(r"^(?:@[a-z0-9_]{4,32}|(?:https?://)?(?:[\w-]+\.)+[\w-]{2,}(?:/[\w@+-]*)?)$", re.IGNORECASE)


def _forget(app: App, tg_id: int | None = None) -> None:
    if app.moderator is not None:
        app.moderator.forget(tg_id)


def parse_list(text: str) -> list[str]:
    """Строки через перенос или запятую, без пустых и повторов."""
    items = [" ".join(item.split()) for item in re.split(r"[\n,;]+", text)]
    return list(dict.fromkeys(item for item in items if item))


async def _protection(app: App, event: Message | CallbackQuery, chat_id: int, note: str | None = None) -> None:
    screen = await guard.protection_view(app, chat_id, note)
    await show(app, event, screen or await screens.chats_list(app))


async def _chat_or_gone(callback: CallbackQuery, callback_data: Guard, app: App, callback_answer: CallbackAnswer):
    chat = await app.repo.get_chat(callback_data.id)
    if chat is None:
        callback_answer.text = "Чат уже удалён"
        await show(app, callback, await screens.chats_list(app))
    return chat


# -------------------------------------------------------------------- защита чата


@router.callback_query(Guard.filter(F.a == "chat"))
async def open_protection(
    callback: CallbackQuery, callback_data: Guard, app: App, callback_answer: CallbackAnswer
) -> None:
    if callback_data.id == 0:  # выбор общих каналов открыт из настроек
        await show(app, callback, await guard.sub_settings_view(app))
        return
    if await _chat_or_gone(callback, callback_data, app, callback_answer):
        await _protection(app, callback, callback_data.id)


@router.callback_query(Guard.filter(F.a == "spam"))
async def toggle_spam(callback: CallbackQuery, callback_data: Guard, app: App, callback_answer: CallbackAnswer) -> None:
    chat = await _chat_or_gone(callback, callback_data, app, callback_answer)
    if chat is None:
        return
    on = bool(callback_data.v)
    await app.repo.update_chat(chat.id, spam_filter={**spam.SpamRules.of(chat.spam_filter).to_json(), "on": on})
    _forget(app, chat.tg_id)
    if on and chat.can_delete is False:
        callback_answer.text = "⚠️ У бота нет права «Удаление сообщений» в этом чате — без него защита не работает"
        callback_answer.show_alert = True
    await _protection(app, callback, chat.id)


@router.callback_query(Guard.filter(F.a.startswith("r_")))
async def toggle_rule(callback: CallbackQuery, callback_data: Guard, app: App, callback_answer: CallbackAnswer) -> None:
    rule = callback_data.a[2:]
    chat = await _chat_or_gone(callback, callback_data, app, callback_answer)
    if chat is None or rule not in spam.RULES:
        return
    rules = spam.SpamRules.of(chat.spam_filter).to_json()
    await app.repo.update_chat(chat.id, spam_filter={**rules, rule: bool(callback_data.v)})
    _forget(app, chat.tg_id)
    await _protection(app, callback, chat.id)


@router.callback_query(Guard.filter(F.a == "sm"))
async def set_sub_mode(
    callback: CallbackQuery, callback_data: Guard, app: App, callback_answer: CallbackAnswer
) -> None:
    chat = await _chat_or_gone(callback, callback_data, app, callback_answer)
    if chat is None:
        return
    mode = guard.SUB_MODES.get(callback_data.v)
    values: dict[str, object] = {"sub_mode": mode}
    note = None
    if mode == "own":
        if chat.sub_channels is None:  # для начала — те же, что общие
            values["sub_channels"] = list(app.settings.sub_channels)
        note = "👉 Отметьте каналы для этого чата кнопкой «📢 Каналы этого чата»."
    await app.repo.update_chat(chat.id, **values)
    _forget(app, chat.tg_id)
    await _protection(app, callback, chat.id, note)


@router.callback_query(Guard.filter(F.a == "log"))
async def open_log(callback: CallbackQuery, callback_data: Guard, app: App, callback_answer: CallbackAnswer) -> None:
    screen = await guard.log_view(app, callback_data.id)
    if screen is None:
        callback_answer.text = "Чат уже удалён"
    await show(app, callback, screen or await screens.chats_list(app))


# --------------------------------------------------------------- выбор каналов


async def _channels(app: App, event: Message | CallbackQuery, chat_id: int, note: str | None = None) -> None:
    screen = await guard.channels_view(app, chat_id, note)
    await show(app, event, screen or await screens.chats_list(app))


@router.callback_query(Guard.filter(F.a == "ch"))
async def open_channels(callback: CallbackQuery, callback_data: Guard, app: App) -> None:
    await _channels(app, callback, callback_data.id)


@router.callback_query(Guard.filter(F.a.in_({"con", "coff"})))
async def toggle_channel(
    callback: CallbackQuery, callback_data: Guard, app: App, callback_answer: CallbackAnswer
) -> None:
    on = callback_data.a == "con"
    channel = await app.repo.get_chat(callback_data.v)
    if on and (channel is None or channel.type != "channel" or channel.status != "active"):
        callback_answer.text = "Этот канал недоступен боту"
        callback_answer.show_alert = True
        return
    if callback_data.id:
        chat = await app.repo.get_chat(callback_data.id)
        if chat is None:
            callback_answer.text = "Чат уже удалён"
            await show(app, callback, await screens.chats_list(app))
            return
        ids = [cid for cid in chat.sub_channels or [] if cid != callback_data.v]
        await app.repo.update_chat(chat.id, sub_channels=[*ids, callback_data.v] if on else ids)
    else:
        ids = [cid for cid in app.settings.sub_channels if cid != callback_data.v]
        await app.settings.set_list(app.repo, "sub_channels", [*ids, callback_data.v] if on else ids)
    _forget(app)
    note = None
    # Закрытому каналу сразу создаём запасную общую ссылку: она нужна, если личную Telegram создать не даст.
    # Если права на ссылки нет вовсе, экран каналов сам это покажет (guard.channel_problem).
    private = on and channel is not None and not channel.username and channel.can_invite is not False
    if private and app.moderator is not None and await app.moderator.reserve_link(channel) is None:
        note = f"⚠️ Не удалось создать ссылку на канал: дайте боту право {t.INVITE_RIGHT}."
    await _channels(app, callback, callback_data.id, note)


# ---------------------------------------------------------------- общие настройки


@router.callback_query(Guard.filter(F.a == "spam_set"))
async def open_spam_settings(callback: CallbackQuery, app: App) -> None:
    await show(app, callback, await guard.spam_settings_view(app))


@router.callback_query(Guard.filter(F.a == "spam_all"))
async def spam_everywhere(
    callback: CallbackQuery, callback_data: Guard, app: App, callback_answer: CallbackAnswer
) -> None:
    on = bool(callback_data.v)
    count = await app.repo.set_spam_everywhere(on)
    _forget(app)
    callback_answer.text = f"🛡 Антиспам {'включён' if on else 'выключен'} в группах: {count}"
    await show(app, callback, await guard.spam_settings_view(app))


@router.callback_query(Guard.filter(F.a == "words"))
async def ask_words(callback: CallbackQuery, state: FSMContext, app: App) -> None:
    await prompt(
        app,
        callback,
        state,
        Input.spam_words,
        guard.words_prompt_text(app),
        cancel=Guard(a="spam_set"),
        extra=[[btn("↩️ Вернуть стандартный список", Guard(a="words_reset"))]],
    )


@router.message(Input.spam_words, F.text)
async def on_words(message: Message, state: FSMContext, app: App) -> None:
    words = parse_list(message.text)
    too_long = [w for w in words if len(w) > MAX_WORD_LENGTH]
    if not words or too_long or len(words) > MAX_WORDS:
        await message.reply(
            f"⚠️ Нужен список до {MAX_WORDS} слов или фраз, каждая до {MAX_WORD_LENGTH} символов — по одной в строке."
        )
        return
    await app.settings.set_list(app.repo, "spam_words", words)
    await finish_input(app, message, state)
    await show(app, message, await guard.spam_settings_view(app, note=f"✅ Стоп-слов в списке: {len(words)}"))


@router.callback_query(Guard.filter(F.a == "words_reset"))
async def reset_words(callback: CallbackQuery, state: FSMContext, app: App) -> None:
    await state.clear()
    await app.settings.reset_spam_words(app.repo)
    await show(app, callback, await guard.spam_settings_view(app, note="↩️ Вернулся стандартный список стоп-слов"))


@router.callback_query(Guard.filter(F.a == "allow"))
async def ask_allow(callback: CallbackQuery, state: FSMContext, app: App) -> None:
    await prompt(
        app,
        callback,
        state,
        Input.spam_allow,
        guard.allow_prompt_text(app),
        cancel=Guard(a="spam_set"),
        extra=[[btn("🗑 Очистить список", Guard(a="allow_clear"))]],
    )


@router.message(Input.spam_allow, F.text)
async def on_allow(message: Message, state: FSMContext, app: App) -> None:
    items = [item.lower() for item in parse_list(message.text)]
    wrong = [item for item in items if not _ALLOW_RE.match(item)]
    if not items or wrong:
        example = t.esc(wrong[0], 60) if wrong else ""
        await message.reply(
            "⚠️ Не похоже на домен или канал"
            + (f": <code>{example}</code>" if example else "")
            + ".\nПримеры: <code>mysite.ru</code>, <code>@mychannel</code>, <code>t.me/mychannel</code>."
        )
        return
    await app.settings.set_list(app.repo, "spam_allow", items[:MAX_WORDS])
    _forget(app)
    await finish_input(app, message, state)
    await show(app, message, await guard.spam_settings_view(app, note=f"✅ Разрешённых ссылок: {len(items)}"))


@router.callback_query(Guard.filter(F.a == "allow_clear"))
async def clear_allow(callback: CallbackQuery, state: FSMContext, app: App) -> None:
    await state.clear()
    await app.settings.set_list(app.repo, "spam_allow", [])
    _forget(app)
    await show(app, callback, await guard.spam_settings_view(app, note="🗑 Список разрешённых ссылок очищен"))


@router.callback_query(Guard.filter(F.a == "sub"))
async def open_sub_settings(callback: CallbackQuery, app: App) -> None:
    await show(app, callback, await guard.sub_settings_view(app))


@router.callback_query(Guard.filter(F.a == "sub_reset"))
async def reset_sub_modes(callback: CallbackQuery, app: App, callback_answer: CallbackAnswer) -> None:
    count = await app.repo.reset_sub_modes()
    _forget(app)
    callback_answer.text = f"↩️ Перешли на общие каналы: {count}"
    await show(app, callback, await guard.sub_settings_view(app))


# ---------------------------------------------------------------- автоприём заявок


@router.callback_query(Guard.filter(F.a == "joins"))
async def open_joins(callback: CallbackQuery, app: App) -> None:
    await show(app, callback, await guard.joins_view(app))


@router.callback_query(Guard.filter(F.a.in_({"jn", "jchat"})))
async def toggle_join(callback: CallbackQuery, callback_data: Guard, app: App, callback_answer: CallbackAnswer) -> None:
    """jn — галочка на экране автоприёма, jchat — переключатель на экране чата."""
    chat = await _chat_or_gone(callback, callback_data, app, callback_answer)
    if chat is None:
        return
    on = bool(callback_data.v)
    await app.repo.update_chat(chat.id, auto_approve=on)
    if on and chat.can_invite is False:
        callback_answer.text = f"⚠️ Нет права {t.invite_right(chat.type)} — без него заявки не приходят боту"
        callback_answer.show_alert = True
    else:
        callback_answer.text = "🚪 Автоприём включён" if on else "⏸ Автоприём выключен — заявки принимаете вы"
    if callback_data.a == "jchat":
        await show(app, callback, await screens.chat_view(app, chat.id) or await screens.chats_list(app))
    else:
        await show(app, callback, await guard.joins_view(app))


@router.callback_query(Guard.filter(F.a == "jall"))
async def join_everywhere(callback: CallbackQuery, callback_data: Guard, app: App) -> None:
    """Общая настройка — для всех чатов и для новых; выбранное отдельно для чатов сбрасывается."""
    on = bool(callback_data.v)
    await app.settings.set(app.repo, "join_auto", on)
    await app.repo.reset_auto_approve()
    note = "✅ Автоприём включён во всех чатах и каналах" if on else "⏸ Автоприём выключен во всех чатах и каналах"
    await show(app, callback, await guard.joins_view(app, note))


# ---------------------------------------------------------------- скрытые админы


async def _trusted(app: App, event: Message | CallbackQuery, chat_id: int, note: str | None = None) -> None:
    screen = await guard.trusted_view(app, chat_id, note)
    await show(app, event, screen or await screens.chats_list(app))


async def _add_trusted(app: App, chat_id: int, entries: list[trusted.Entry]) -> list[trusted.Entry] | None:
    """Добавляет в общий список (chat_id=0) или в список чата. None — чат уже удалён."""
    if chat_id:
        chat = await app.repo.get_chat(chat_id)
        if chat is None:
            return None
        merged, added = trusted.merge(trusted.clean_list(chat.trusted), entries)
        await app.repo.update_chat(chat.id, trusted=merged)
        _forget(app, chat.tg_id)
    else:
        merged, added = trusted.merge(app.settings.trusted, entries)
        await app.settings.set_list(app.repo, "trusted", merged)
        _forget(app)
    return added


async def _add_and_show(app: App, event: Message | CallbackQuery, chat_id: int, entries: list[trusted.Entry]) -> None:
    added = await _add_trusted(app, chat_id, entries)
    if added is None:
        await show(app, event, await screens.chats_list(app))
        return
    if added:
        note = "✅ Добавлены: " + t.names_label([trusted.label(entry) for entry in added], 10, 40)
    elif len(await _current_trusted(app, chat_id)) >= trusted.MAX_TRUSTED:
        note = f"⚠️ В списке уже {trusted.MAX_TRUSTED} — уберите лишних, чтобы добавить новых."
    else:
        note = "Они уже в списке."
    await _trusted(app, event, chat_id, note)


async def _current_trusted(app: App, chat_id: int) -> list[trusted.Entry]:
    if not chat_id:
        return app.settings.trusted
    chat = await app.repo.get_chat(chat_id)
    return trusted.clean_list(chat.trusted) if chat else []


@router.callback_query(Guard.filter(F.a == "trust"))
async def open_trusted(callback: CallbackQuery, callback_data: Guard, app: App) -> None:
    await _trusted(app, callback, callback_data.id)


@router.callback_query(Guard.filter(F.a == "tadd"))
async def ask_trusted(
    callback: CallbackQuery, callback_data: Guard, state: FSMContext, app: App, callback_answer: CallbackAnswer
) -> None:
    chat = None
    if callback_data.id:
        chat = await _chat_or_gone(callback, callback_data, app, callback_answer)
        if chat is None:
            return
    await prompt(
        app,
        callback,
        state,
        Input.trusted,
        guard.trusted_prompt_text(chat),
        cancel=Guard(a="trust", id=callback_data.id),
        target=callback_data.id,
    )


@router.message(F.users_shared)
async def on_users_shared(message: Message, state: FSMContext, app: App) -> None:
    """Люди выбраны кнопкой «🕶 Добавить скрытого админа» внизу экрана. Если админ как раз добавляет
    скрытых админов в чат (или во все группы) — туда, иначе бот спросит, куда."""
    entries = [trusted.from_shared(user) for user in message.users_shared.users]
    if not entries:
        return
    if await state.get_state() == Input.trusted.state:
        chat_id = int((await state.get_data()).get("target") or 0)
        await finish_input(app, message, state)
        await _add_and_show(app, message, chat_id, entries)
        return
    app.picked[message.from_user.id] = entries
    await show(app, message, await guard.trusted_target_view(app, entries))


@router.callback_query(Guard.filter(F.a == "tput"))
async def put_picked(callback: CallbackQuery, callback_data: Guard, app: App, callback_answer: CallbackAnswer) -> None:
    entries = app.picked.pop(callback.from_user.id, None)
    if not entries:
        callback_answer.text = "Выбор устарел — выберите людей ещё раз кнопкой «🕶 Добавить скрытого админа» внизу"
        callback_answer.show_alert = True
        return
    await _add_and_show(app, callback, callback_data.id, entries)


@router.message(Input.trusted)
async def on_trusted(message: Message, state: FSMContext, app: App) -> None:
    """Пересланное сообщение человека (или поста канала), либо @username / ID текстом."""
    if message.forward_origin is not None:
        entry = trusted.from_forward(message.forward_origin)
        if entry is None:
            await message.reply(
                "⚠️ Этот человек скрыл пересылку своих сообщений — бот не видит, кто это. Выберите его кнопкой "
                "«🕶 Добавить скрытого админа» внизу экрана или пришлите @username."
            )
            return
        entries = [entry]
    elif message.text:
        entries, wrong = trusted.parse_text(message.text)
        if wrong or not entries:
            example = t.esc(wrong[0], 60) if wrong else ""
            await message.reply(
                "⚠️ Не похоже на @username или ID"
                + (f": <code>{example}</code>" if example else "")
                + ".\nПримеры: <code>@ivan_petrov</code>, <code>123456789</code>."
            )
            return
    else:
        await message.reply(
            "Перешлите сообщение этого человека, пришлите @username или ID — или выберите его кнопкой внизу экрана."
        )
        return
    chat_id = int((await state.get_data()).get("target") or 0)
    await finish_input(app, message, state)
    await _add_and_show(app, message, chat_id, entries)


@router.callback_query(Guard.filter(F.a == "tdel"))
async def remove_trusted(
    callback: CallbackQuery, callback_data: Guard, app: App, callback_answer: CallbackAnswer
) -> None:
    if callback_data.id:
        chat = await _chat_or_gone(callback, callback_data, app, callback_answer)
        if chat is None:
            return
        entries, removed = trusted.remove(trusted.clean_list(chat.trusted), callback_data.v)
        if removed:
            await app.repo.update_chat(chat.id, trusted=entries)
            _forget(app, chat.tg_id)
    else:
        entries, removed = trusted.remove(app.settings.trusted, callback_data.v)
        if removed:
            await app.settings.set_list(app.repo, "trusted", entries)
            _forget(app)
    callback_answer.text = f"Убран: {trusted.label(removed)}"[:200] if removed else "Уже убран"
    await _trusted(app, callback, callback_data.id)
