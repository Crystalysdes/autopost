"""Посты: приём пачкой, карточка поста, кнопки, замена содержимого, режим пересылки."""

from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.services.buttons import ButtonParseError, buttons_to_html, count_buttons, parse_buttons
from bot.services.content import ContentError, capture, custom_emoji_count, post_title, supports_buttons
from bot.services.errors import humanize
from bot.services.preview import send_preview
from bot.services.sender import PostData, send_post
from bot.states import Input
from bot.ui import keyboards, screens
from bot.ui import texts as t
from bot.ui.callbacks import CampAct, Nav, PostAct
from bot.ui.keyboards import GREEN, btn
from bot.ui.render import finish_input, input_value, prompt, show

router = Router(name="posts")
router.message.filter(F.chat.type == "private")

ADD_POSTS_TEXT = (
    "<b>➕ Добавление постов</b>\n\n"
    "Пришлите сообщения, которые бот будет публиковать, — по одному посту на сообщение:\n"
    "• текст, фото, видео, GIF, файл, аудио, голосовое, кружок, стикер;\n"
    "• альбом (несколько фото/видео одним сообщением);\n"
    "• или перешлите пост из своего канала.\n\n"
    "Форматирование, спойлеры, подпись над медиа и премиум-эмодзи сохранятся как есть. "
    "Кнопки добавите потом в карточке поста.\n\n"
    "Когда закончите — нажмите «✅ Готово»."
)

BUTTONS_HELP = (
    "<b>🔘 Кнопки под постом</b>\n\n"
    "Одна строка — один ряд кнопок, «|» разделяет кнопки в ряду:\n"
    "<code>Текст - https://ссылка</code>\n"
    "<code>Кнопка 1 - https://a.ru | Кнопка 2 - t.me/channel</code>\n"
    "<code>Купить - https://shop.ru - green</code>  (цвет: green, red, blue)\n"
    "<code>Промокод - copy:SALE2026</code>  (кнопка копирует текст)\n\n"
    "Премиум-эмодзи в начале текста кнопки станет её иконкой."
)


async def _gone(callback: CallbackQuery, app: App, callback_answer: CallbackAnswer) -> None:
    callback_answer.text = "Это уже удалено"
    await show(app, callback, await screens.main_menu(app))


# ------------------------------------------------------------------ приём постов


@router.callback_query(CampAct.filter(F.a == "add"))
async def start_adding(
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
        Input.posts,
        ADD_POSTS_TEXT,
        cancel=Nav(to="posts", id=campaign.id),
        extra=[[btn("✅ Готово", CampAct(a="done", id=campaign.id), GREEN)]],
        camp_id=campaign.id,
    )


@router.message(Input.posts)
async def on_post_message(
    message: Message,
    state: FSMContext,
    app: App,
    album: list[Message] | None = None,
    fsm_snapshot: dict[str, Any] | None = None,
) -> None:
    campaign_id = await input_value(state, "camp_id", fsm_snapshot)
    if campaign_id is None or await app.repo.get_campaign(campaign_id) is None:
        await state.clear()
        await message.answer("Не понял, в какую рассылку добавить пост. Откройте её и нажмите «➕ Добавить посты».")
        return
    try:
        captured = capture(message, album)
    except ContentError as error:
        await message.reply(f"⚠️ {t.esc(error)}")
        return
    post, number = await app.repo.add_post(
        campaign_id,
        kind=captured.kind,
        payload=captured.payload,
        buttons=captured.buttons,
        forward_from=captured.forward_from,
    )
    notes = []
    if captured.buttons:
        notes.append(f"импортировано кнопок: {count_buttons(captured.buttons)}")
    if captured.forward_from:
        notes.append("пост переслан — в его карточке можно включить публикацию пересылкой")
    if custom_emoji_count(post.kind, post.payload):
        notes.append("есть премиум-эмодзи — проверьте их через «👁 Предпросмотр»")
    text = f"✅ Пост #{number} добавлен: {post_title(post.kind, post.payload)}"
    if notes:
        text += "\n" + "\n".join(f"• {note}" for note in notes)
    text += "\n\nПришлите ещё или нажмите «✅ Готово»."
    await message.reply(text, reply_markup=keyboards.done_adding(campaign_id))


@router.callback_query(CampAct.filter(F.a == "done"))
async def done_adding(
    callback: CallbackQuery,
    callback_data: CampAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    await state.clear()
    screen = await screens.posts_view(app, callback_data.id)
    if screen is None:
        return await _gone(callback, app, callback_answer)
    await show(app, callback, screen)


@router.callback_query(CampAct.filter(F.a.in_({"rot_seq", "rot_rnd"})))
async def set_rotation(
    callback: CallbackQuery, callback_data: CampAct, app: App, callback_answer: CallbackAnswer
) -> None:
    campaign = await app.repo.get_campaign(callback_data.id)
    if campaign is None:
        return await _gone(callback, app, callback_answer)
    rotation = "random" if callback_data.a == "rot_rnd" else "sequential"
    await app.repo.update_campaign(campaign.id, rotation=rotation)
    callback_answer.text = "Порядок: " + ("случайно" if rotation == "random" else "по очереди")
    await show(app, callback, await screens.posts_view(app, campaign.id))


# ----------------------------------------------------------------- карточка поста


@router.callback_query(PostAct.filter(F.a == "show"))
async def show_post(callback: CallbackQuery, callback_data: PostAct, app: App, callback_answer: CallbackAnswer) -> None:
    post = await app.repo.get_post(callback_data.id)
    if post is None:
        return await _gone(callback, app, callback_answer)
    callback_answer.disabled = True
    await callback.answer()
    note = await send_preview(app, [post], callback.from_user.id)
    screen = await screens.post_view(app, post.id, note=note or "👆 Так пост будет выглядеть в чате.")
    if screen:
        await show(app, callback, screen, new=True)


@router.callback_query(PostAct.filter(F.a == "btn"))
async def ask_buttons(
    callback: CallbackQuery,
    callback_data: PostAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    post = await app.repo.get_post(callback_data.id)
    if post is None:
        return await _gone(callback, app, callback_answer)
    if not supports_buttons(post.kind):
        callback_answer.text = "К альбомам Telegram не позволяет добавлять кнопки"
        callback_answer.show_alert = True
        return
    text = BUTTONS_HELP
    extra = []
    if post.buttons:
        text += "\n\n<b>Сейчас:</b>\n" + buttons_to_html(post.buttons)
        extra.append([btn("🗑 Убрать кнопки", PostAct(a="btndel", id=post.id))])
    await prompt(
        app,
        callback,
        state,
        Input.buttons,
        text,
        cancel=Nav(to="post", id=post.id),
        extra=extra,
        post_id=post.id,
    )


@router.message(Input.buttons, F.text)
async def on_buttons(message: Message, state: FSMContext, app: App) -> None:
    post_id = await input_value(state, "post_id")
    post = await app.repo.get_post(post_id) if post_id is not None else None
    if post is None:
        await state.clear()
        await message.answer("Пост уже удалён.")
        return
    try:
        rows = parse_buttons(message.text, message.entities)
    except ButtonParseError as error:
        await message.reply(f"⚠️ {t.esc(error)}\n\nИсправьте и пришлите снова.")
        return
    candidate = PostData.of(post)
    candidate.buttons = rows
    try:
        # Предпросмотр заодно проверяет ссылки: неправильные Telegram отклонит
        await send_post(app.bot, message.chat.id, candidate)
    except TelegramBadRequest as error:
        await message.reply(f"⚠️ Telegram не принял кнопки: {t.esc(humanize(error))}\n\nИсправьте и пришлите снова.")
        return
    except TelegramAPIError:
        pass
    await app.repo.update_post(post.id, buttons=rows)
    await finish_input(app, message, state)
    note = f"✅ Кнопки сохранены ({count_buttons(rows)}). Выше — как будет выглядеть пост."
    await show(app, message, await screens.post_view(app, post.id, note=note))


@router.callback_query(PostAct.filter(F.a == "btndel"))
async def remove_buttons(
    callback: CallbackQuery,
    callback_data: PostAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    await state.clear()
    post = await app.repo.update_post(callback_data.id, buttons=None)
    if post is None:
        return await _gone(callback, app, callback_answer)
    callback_answer.text = "Кнопки убраны"
    await show(app, callback, await screens.post_view(app, post.id))


@router.callback_query(PostAct.filter(F.a == "replace"))
async def ask_replace(
    callback: CallbackQuery,
    callback_data: PostAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    post = await app.repo.get_post(callback_data.id)
    if post is None:
        return await _gone(callback, app, callback_answer)
    await prompt(
        app,
        callback,
        state,
        Input.replace,
        "🔄 Пришлите новое содержимое поста: текст, медиа или альбом. Кнопки сохранятся.",
        cancel=Nav(to="post", id=post.id),
        post_id=post.id,
    )


@router.message(Input.replace)
async def on_replace(
    message: Message,
    state: FSMContext,
    app: App,
    album: list[Message] | None = None,
    fsm_snapshot: dict[str, Any] | None = None,
) -> None:
    post_id = await input_value(state, "post_id", fsm_snapshot)
    post = await app.repo.get_post(post_id) if post_id is not None else None
    if post is None:
        await state.clear()
        await message.answer("Пост уже удалён.")
        return
    try:
        captured = capture(message, album)
    except ContentError as error:
        await message.reply(f"⚠️ {t.esc(error)}")
        return
    values: dict[str, object] = {
        "kind": captured.kind,
        "payload": captured.payload,
        "forward_from": captured.forward_from,
        "send_mode": "copy",
    }
    if captured.buttons and not post.buttons:
        values["buttons"] = captured.buttons
    await app.repo.update_post(post.id, **values)
    await finish_input(app, message, state)
    note = "🔄 Содержимое заменено."
    if post.buttons and not supports_buttons(captured.kind):
        note += " Кнопки сохранены, но к альбому Telegram их не прикрепит."
    await show(app, message, await screens.post_view(app, post.id, note=note))


@router.callback_query(PostAct.filter(F.a.in_({"fwd", "copy"})))
async def set_mode(callback: CallbackQuery, callback_data: PostAct, app: App, callback_answer: CallbackAnswer) -> None:
    post = await app.repo.get_post(callback_data.id)
    if post is None:
        return await _gone(callback, app, callback_answer)
    if not post.forward_from:
        callback_answer.text = "Пересылка доступна только для пересланных постов"
        return
    mode = "forward" if callback_data.a == "fwd" else "copy"
    await app.repo.update_post(post.id, send_mode=mode)
    callback_answer.text = "Режим: пересылка" if mode == "forward" else "Режим: копия"
    note = None
    if mode == "forward":
        note = (
            "↪️ Пост будет пересылаться из вашего чата с ботом с плашкой «Переслано из…». "
            "Не удаляйте исходное сообщение из этого чата — иначе пересылать будет нечего."
        )
    await show(app, callback, await screens.post_view(app, post.id, note=note))


@router.callback_query(PostAct.filter(F.a.in_({"up", "down"})))
async def move_post(callback: CallbackQuery, callback_data: PostAct, app: App, callback_answer: CallbackAnswer) -> None:
    moved = await app.repo.move_post(callback_data.id, -1 if callback_data.a == "up" else 1)
    if not moved:
        callback_answer.text = "Дальше двигать некуда"
    screen = await screens.post_view(app, callback_data.id)
    if screen is None:
        return await _gone(callback, app, callback_answer)
    await show(app, callback, screen)


@router.callback_query(PostAct.filter(F.a == "del"))
async def confirm_delete(
    callback: CallbackQuery, callback_data: PostAct, app: App, callback_answer: CallbackAnswer
) -> None:
    screen = await screens.post_delete_confirm(app, callback_data.id)
    if screen is None:
        return await _gone(callback, app, callback_answer)
    await show(app, callback, screen)


@router.callback_query(PostAct.filter(F.a == "del_ok"))
async def delete_post(
    callback: CallbackQuery, callback_data: PostAct, app: App, callback_answer: CallbackAnswer
) -> None:
    campaign_id = await app.repo.delete_post(callback_data.id)
    if campaign_id is None:
        return await _gone(callback, app, callback_answer)
    callback_answer.text = "🗑 Пост удалён"
    await show(app, callback, await screens.posts_view(app, campaign_id) or await screens.main_menu(app))
