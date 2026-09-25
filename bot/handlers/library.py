"""«🗂 Мои посты»: новый пост, новая рассылка из поста, копирование поста в другую рассылку."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.callback_answer import CallbackAnswer

from bot.app import App
from bot.handlers.posts import ADD_POSTS_TEXT
from bot.states import Input
from bot.ui import screens
from bot.ui import texts as t
from bot.ui.callbacks import CampAct, LibAct
from bot.ui.keyboards import GREEN, btn
from bot.ui.render import prompt, show

router = Router(name="library")

NEW_POST_DONE_NOTE = "👉 Теперь отметьте чаты («💬 Чаты»), задайте время («⏰ Расписание») и нажмите «▶️ Запустить»."
NO_POSTS_NOTE = "Постов пока нет — добавьте их в «📝 Посты». Если рассылка не нужна, удалите её кнопкой «🗑 Удалить»."
NEW_FROM_POST_NOTE = (
    "✨ Создана рассылка «{name}» с этим постом. Отметьте чаты, в которые публиковать, "
    "затем задайте время в «⏰ Расписание» и нажмите «▶️ Запустить»."
)


async def _post_gone(callback: CallbackQuery, app: App, callback_answer: CallbackAnswer) -> None:
    callback_answer.text = "Пост уже удалён"
    await show(app, callback, await screens.library_view(app))


async def _next_name(app: App) -> str:
    return f"Рассылка {await app.repo.count_campaigns() + 1}"


@router.callback_query(LibAct.filter(F.a == "add"))
async def new_post(callback: CallbackQuery, state: FSMContext, app: App) -> None:
    """Пост всегда принадлежит рассылке, поэтому новый пост сразу получает свою рассылку."""
    campaign = await app.repo.create_campaign(await _next_name(app))
    await prompt(
        app,
        callback,
        state,
        Input.posts,
        ADD_POSTS_TEXT + f"\n\nПосты попадут в новую рассылку «{t.esc(campaign.name)}».",
        # created_ts в кнопке: старая «Отмена» не удалит другую рассылку, получившую тот же id
        cancel=LibAct(a="drop", id=campaign.id, v=campaign.created_ts),
        extra=[[btn("✅ Готово", CampAct(a="fin", id=campaign.id), GREEN)]],
        camp_id=campaign.id,
        done="fin",
    )


async def drop_if_empty(app: App, campaign_id: int, created_ts: int) -> bool:
    """Рассылку, созданную под новый пост, убираем, если в неё так ничего и не добавили."""
    campaign = await app.repo.get_campaign(campaign_id)
    if campaign is None or campaign.created_ts != created_ts or campaign.is_draft or campaign.is_active:
        return False
    if await app.repo.list_posts(campaign.id) or await app.repo.campaign_targets(campaign.id):
        return False
    await app.repo.delete_campaign(campaign.id)
    return True


@router.callback_query(LibAct.filter(F.a == "drop"))
async def cancel_new_post(callback: CallbackQuery, callback_data: LibAct, app: App) -> None:
    if await drop_if_empty(app, callback_data.id, callback_data.v):
        await show(app, callback, await screens.library_view(app))
        return
    screen = await screens.campaign_view(app, callback_data.id, user_id=callback.from_user.id)
    await show(app, callback, screen or await screens.library_view(app))


@router.callback_query(CampAct.filter(F.a == "fin"))
async def done_new_post(
    callback: CallbackQuery,
    callback_data: CampAct,
    state: FSMContext,
    app: App,
    callback_answer: CallbackAnswer,
) -> None:
    """«Готово» после «➕ Новый пост»: дальше — выбрать чаты и время. Рассылку здесь не удаляем, даже
    если постов нет: альбом, присланный перед нажатием, ещё может собираться и сохранится через миг."""
    await state.clear()
    posts = await app.repo.list_posts(callback_data.id)
    note = NEW_POST_DONE_NOTE if posts else NO_POSTS_NOTE
    screen = await screens.campaign_view(app, callback_data.id, note=note, user_id=callback.from_user.id)
    if screen is None:
        callback_answer.text = "Рассылка уже удалена"
    await show(app, callback, screen or await screens.library_view(app))


@router.callback_query(LibAct.filter(F.a == "new"))
async def campaign_from_post(
    callback: CallbackQuery, callback_data: LibAct, app: App, callback_answer: CallbackAnswer
) -> None:
    campaign = await app.repo.campaign_from_post(callback_data.id, await _next_name(app))
    if campaign is None:
        return await _post_gone(callback, app, callback_answer)
    app.remember_origin(callback.from_user.id, campaign.id, -1)
    note = NEW_FROM_POST_NOTE.format(name=t.esc(campaign.name))
    await show(app, callback, await screens.targets_view(app, campaign.id, note=note))


@router.callback_query(LibAct.filter(F.a == "to"))
async def choose_campaign(
    callback: CallbackQuery, callback_data: LibAct, app: App, callback_answer: CallbackAnswer
) -> None:
    screen = await screens.copy_post_view(app, callback_data.id, callback_data.v, callback_data.f)
    if screen is None:
        return await _post_gone(callback, app, callback_answer)
    await show(app, callback, screen)


@router.callback_query(LibAct.filter(F.a == "cp"))
async def copy_post(callback: CallbackQuery, callback_data: LibAct, app: App, callback_answer: CallbackAnswer) -> None:
    target = await app.repo.get_campaign(callback_data.v)
    copied = await app.repo.copy_post(callback_data.id, callback_data.v) if target else None
    if copied is None:
        if await app.repo.get_post(callback_data.id) is None:
            return await _post_gone(callback, app, callback_answer)
        callback_answer.text = "Эта рассылка уже удалена"
        screen = await screens.copy_post_view(app, callback_data.id, 0, callback_data.f)
        await show(app, callback, screen or await screens.library_view(app))
        return
    _post, number = copied
    where = "черновик" if target.is_draft else "рассылку"
    callback_answer.text = f"📋 Скопировано в {where} «{target.name}» (пост #{number})"
    note = f"📋 Копия добавлена в {where} «{t.esc(target.name)}» — там это пост #{number}."
    screen = await screens.post_view(app, callback_data.id, note=note, origin=callback_data.f)
    await show(app, callback, screen or await screens.library_view(app))
