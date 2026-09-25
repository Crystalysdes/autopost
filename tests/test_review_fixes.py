"""Регрессионные тесты на найденные при ревью ошибки."""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramMigrateToChat
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import DeleteMessages, PinChatMessage, SendMessage, UnpinChatMessage

from bot.db.repo import DRAFT
from bot.handlers.settings import parse_timezone
from bot.middlewares.taps import DoubleTapMiddleware
from bot.services import scheduler as scheduler_module
from bot.services.content import ContentError, capture
from bot.services.scheduler import Scheduler
from bot.states import Input
from bot.storage import LeanMemoryStorage
from bot.ui.callbacks import CampAct, ChatAct, PickAct, SetAct
from tests.conftest import OWNER_ID, STRANGER_ID, FakeClock
from tests.test_flows import dp, feed, group_message, membership, press, private_message, state_of  # noqa: F401

MSK = ZoneInfo("Europe/Moscow")
CHAT_TG = -1002222222222


def at(hour: int, minute: int = 0, day: int = 25) -> float:
    return datetime(2026, 9, day, hour, minute, tzinfo=MSK).timestamp()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(at(8, 0))


@pytest.fixture
def scheduler(app, clock, monkeypatch) -> Scheduler:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", no_sleep)
    instance = Scheduler(app, clock=clock, rng=random.Random(0))
    app.scheduler = instance
    return instance


async def make_campaign(app, *, times=("12:00",), posts=("Пост",), chat_tg=CHAT_TG, chat_type="supergroup", **options):
    change = await app.repo.upsert_chat_membership(
        tg_id=chat_tg,
        title="Чат",
        username=None,
        chat_type=chat_type,
        is_forum=False,
        in_chat=True,
        is_admin=True,
        can_post=True,
        can_pin=True,
        actor_id=OWNER_ID,
        admin_ids=[OWNER_ID],
    )
    campaign = await app.repo.create_campaign("R", chat_ids=[change.chat.id])
    for text in posts:
        await app.repo.add_post(campaign.id, kind="text", payload={"text": text})
    await app.repo.update_campaign(campaign.id, times=list(times), is_active=True, **options)
    await app.scheduler.reschedule([campaign.id])
    return await app.repo.get_campaign(campaign.id), change.chat


async def run_tick(scheduler: Scheduler) -> None:
    await scheduler.tick()
    await scheduler.drain()


def sent(session, chat_tg=CHAT_TG):
    return [c for c in session.calls(SendMessage) if c.chat_id == chat_tg]


# ------------------------------------------------------------ 1. двойные нажатия


async def test_double_tap_middleware_drops_repeats():
    middleware = DoubleTapMiddleware(window=10)
    calls = []

    async def handler(event, data):
        calls.append(event.data)
        await asyncio.sleep(0.01)

    async def answer(*_args, **_kwargs):
        return True

    def tap(data="k:new:1", user=1):
        from aiogram.types import CallbackQuery, User

        query = CallbackQuery(
            id="1", from_user=User(id=user, is_bot=False, first_name="A"), chat_instance="c", data=data
        )
        object.__setattr__(query, "answer", answer)
        return query

    await asyncio.gather(middleware(handler, tap(), {}), middleware(handler, tap(), {}))  # одновременно
    await middleware(handler, tap(), {})  # сразу после — в пределах окна
    await middleware(handler, tap(user=2), {})  # другой пользователь — отдельно
    await middleware(handler, tap("k:new:2"), {})  # другая кнопка — отдельно
    assert calls == ["k:new:1", "k:new:1", "k:new:2"]


async def test_concurrent_apply_creates_one_campaign(dp, bot, app):  # noqa: F811
    await feed_updates(dp, bot, membership(-103001), membership(-103002))
    chat_b = await app.repo.get_chat_by_tg(-103002)
    draft = await app.repo.create_campaign("R", kind=DRAFT)
    await app.repo.add_post(draft.id, kind="text", payload={"text": "x"})
    await app.repo.update_campaign(draft.id, times=["12:00"])
    await feed_updates(dp, bot, press(CampAct(a="apply", id=draft.id)), press(PickAct(a="t", s=draft.id, v=chat_b.id)))
    go = PickAct(a="go", s=draft.id, v=1)
    await asyncio.gather(dp.feed_update(bot, press(go)), dp.feed_update(bot, press(go)))
    assert len(await app.repo.list_campaigns()) == 1
    assert len(await app.repo.list_campaigns(chat_b.id)) == 1


async def feed_updates(dp, bot, *updates):  # noqa: F811
    for update in updates:
        await dp.feed_update(bot, update)


async def test_concurrent_send_now_sends_once(app, scheduler, session):
    campaign, _ = await make_campaign(app, posts=("A", "B"))
    results = await asyncio.gather(scheduler.send_now(campaign.id), scheduler.send_now(campaign.id))
    assert sorted(ok for ok, _ in results) == [False, True]
    assert [c.text for c in sent(session)] == ["A"]


# ------------------------------------------- 2–3, 11. пересчёт расписания и разброс


async def test_edit_after_early_jitter_send_does_not_repeat_slot(app, scheduler, clock, session):
    campaign, _ = await make_campaign(app, times=("12:00",), jitter_min=30)
    await app.repo.update_campaign(campaign.id, next_slot_ts=int(at(12)), next_run_ts=int(at(11, 39)))
    clock.value = at(11, 39) + 1
    await run_tick(scheduler)
    assert len(sent(session)) == 1
    clock.value = at(11, 41)
    await scheduler.reschedule([campaign.id])  # например, правка расписания
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.next_slot_ts == int(at(12, day=26))


async def test_edit_does_not_lose_slot_with_late_jitter(app, scheduler, clock, session):
    campaign, _ = await make_campaign(app, times=("09:00", "12:00"), jitter_min=30)
    await app.repo.update_campaign(
        campaign.id, last_slot_ts=int(at(9)), next_slot_ts=int(at(12)), next_run_ts=int(at(12, 27))
    )
    clock.value = at(12, 26)
    await scheduler.reschedule([campaign.id])
    fresh = await app.repo.get_campaign(campaign.id)
    assert fresh.next_slot_ts == int(at(12))
    clock.value = fresh.next_run_ts + 1
    await run_tick(scheduler)
    assert len(sent(session)) == 1


async def test_schedule_edit_keeps_campaign_running(app, scheduler):
    campaign, _ = await make_campaign(app)
    await app.repo.update_campaign(campaign.id, weekdays=[])
    await scheduler.reschedule([campaign.id])
    paused = await app.repo.get_campaign(campaign.id)
    assert paused.is_active and paused.next_run_ts is None
    await app.repo.update_campaign(campaign.id, weekdays=[0, 1, 2, 3, 4, 5, 6])
    await scheduler.reschedule([campaign.id])
    resumed = await app.repo.get_campaign(campaign.id)
    assert resumed.is_active and resumed.next_run_ts


async def test_no_burst_after_short_restart(app, scheduler, clock, session):
    await make_campaign(app, times=("12:00", "12:05", "12:10", "12:15"))
    clock.value = at(12, 14)  # бот лежал с 12:01 до 12:14: слоты 12:00 (запланирован) и 12:05, 12:10 прошли
    for _ in range(3):
        await run_tick(scheduler)
        clock.advance(10)
    assert len(sent(session)) == 1  # один догоняющий пост, дальше — 12:15


# ------------------------------------------------------------ 4. часовой пояс


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("UTC+3", "Etc/GMT-3"),
        ("utc -5", "Etc/GMT+5"),
        ("+0", "UTC"),
        ("−2", "Etc/GMT+2"),
        ("europe/berlin", "Europe/Berlin"),
    ],
)
def test_parse_timezone_ok(text, expected):
    assert parse_timezone(text) == expected


@pytest.mark.parametrize("text", ["UTC 10", "12", "GMT 12", "UTC 03", "Mars/Olympus"])
def test_parse_timezone_rejects_ambiguous(text):
    with pytest.raises(ValueError):
        parse_timezone(text)


# ------------------------------------------------- 5–6. устаревшие кнопки


async def test_pause_button_sets_explicit_value(dp, bot, app):  # noqa: F811
    await feed_updates(dp, bot, press(SetAct(a="pause", v=1)), press(SetAct(a="pause", v=1)))
    assert app.settings.paused_all is True
    await feed_updates(dp, bot, press(SetAct(a="pause", v=0)))
    assert app.settings.paused_all is False


async def test_stale_leave_button_does_not_remove_accepted_chat(dp, bot, app, session):  # noqa: F811
    await feed_updates(dp, bot, membership(-104001, actor=SimpleUser(STRANGER_ID)))
    chat = await app.repo.get_chat_by_tg(-104001)
    await feed_updates(dp, bot, press(ChatAct(a="accept", id=chat.id)), press(ChatAct(a="leave", id=chat.id)))
    assert (await app.repo.get_chat(chat.id)).status == "active"
    assert not [r for r in session.requests if type(r).__name__ == "LeaveChat"]


def SimpleUser(user_id: int):
    from aiogram.types import User

    return User(id=user_id, is_bot=False, first_name="U")


async def test_accept_does_not_revive_left_chat(dp, bot, app):  # noqa: F811
    await feed_updates(dp, bot, membership(-104002, actor=SimpleUser(STRANGER_ID)))
    chat = await app.repo.get_chat_by_tg(-104002)
    await app.repo.set_chat_status(chat.id, "left")
    await feed_updates(dp, bot, press(ChatAct(a="accept", id=chat.id)))
    assert (await app.repo.get_chat(chat.id)).status == "left"


# ------------------------------------------------ 7, 12. альбомы и сброс ввода


async def test_album_survives_done_pressed_during_wait(dp, bot, app):  # noqa: F811
    await feed_updates(dp, bot, membership(-105001))
    chat = await app.repo.get_chat_by_tg(-105001)
    campaign = await app.repo.create_campaign("R", chat_ids=[chat.id])
    await feed_updates(dp, bot, press(CampAct(a="add", id=campaign.id)))
    parts = [
        private_message(
            media_group_id="alb", photo=[{"file_id": f"f{i}", "file_unique_id": f"u{i}", "width": 1, "height": 1}]
        )
        for i in range(2)
    ]

    async def press_done_soon():
        await asyncio.sleep(0.01)
        await dp.feed_update(bot, press(CampAct(a="done", id=campaign.id)))

    await asyncio.gather(*(dp.feed_update(bot, p) for p in parts), press_done_soon())
    posts = await app.repo.list_posts(campaign.id)
    assert len(posts) == 1 and posts[0].kind == "album"


async def test_button_press_ends_post_input(dp, bot, app):  # noqa: F811
    await feed_updates(dp, bot, membership(-105002))
    chat = await app.repo.get_chat_by_tg(-105002)
    first = await app.repo.create_campaign("Первая", chat_ids=[chat.id])
    await feed_updates(dp, bot, press(CampAct(a="add", id=first.id)))
    assert await state_of(dp, bot) == Input.posts.state
    await feed_updates(dp, bot, press(ChatAct(a="new", id=chat.id)), private_message("случайный текст"))
    assert await state_of(dp, bot) is None
    assert await app.repo.list_posts(first.id) == []


def test_gif_album_rejected():
    from aiogram.types import Message

    def part(i):
        return Message.model_validate(
            {
                "message_id": i,
                "date": 1,
                "chat": {"id": 1, "type": "private"},
                "media_group_id": "g",
                "animation": {"file_id": f"a{i}", "file_unique_id": f"u{i}", "width": 1, "height": 1, "duration": 1},
            }
        )

    with pytest.raises(ContentError, match="GIF"):
        capture(part(1), [part(1), part(2)])


# ---------------------------------------------------- 8–9. закрепы и миграция


async def test_unpin_when_previous_cannot_be_deleted(app, scheduler, clock, session):
    await make_campaign(app, times=("09:00", "10:00"), delete_prev=True, pin=True)
    clock.value = at(9) + 1
    await run_tick(scheduler)
    session.queue(DeleteMessages, TelegramBadRequest(method=None, message="Bad Request: message can't be deleted"))
    clock.value = at(10) + 1
    await run_tick(scheduler)
    unpins = session.calls(UnpinChatMessage)
    assert len(unpins) == 1 and unpins[0].message_id == session.calls(PinChatMessage)[0].message_id


async def test_migration_merge_records_to_new_chat_and_forgets_old_ids(app, scheduler, clock, session):
    campaign, old_chat = await make_campaign(
        app, times=("09:00", "10:00"), chat_tg=-7, chat_type="group", delete_prev=True
    )
    clock.value = at(9) + 1
    await run_tick(scheduler)  # в старой группе ушёл пост — его id запомнен
    # Супергруппа уже зарегистрирована отдельной записью (апдейт пришёл раньше миграции)
    await app.repo.upsert_chat_membership(
        tg_id=-1007007,
        title="Чат",
        username=None,
        chat_type="supergroup",
        is_forum=False,
        in_chat=True,
        is_admin=True,
        can_post=True,
        can_pin=True,
        actor_id=OWNER_ID,
        admin_ids=[OWNER_ID],
    )
    session.queue(SendMessage, TelegramMigrateToChat(method=None, message="migrated", migrate_to_chat_id=-1007007))
    clock.value = at(10) + 1
    await run_tick(scheduler)
    new_chat = await app.repo.get_chat_by_tg(-1007007)
    targets = await app.repo.campaign_targets(campaign.id)
    assert [t.chat.id for t in targets] == [new_chat.id] and targets[0].link.sent_count == 2
    assert not session.calls(DeleteMessages)  # старые id в супергруппе чужие — их не трогаем
    assert await app.repo.get_chat(old_chat.id) is None


# --------------------------------------------------------------- 10. память FSM


async def test_lean_storage_does_not_grow_on_reads():
    storage = LeanMemoryStorage()
    key = StorageKey(bot_id=1, chat_id=-100, user_id=5)
    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {}
    assert await storage.get_value(key, "x", 7) == 7
    await storage.set_state(key, None)
    await storage.set_data(key, {})
    assert storage.storage == {}
    await storage.set_state(key, "S:one")
    await storage.set_data(key, {"a": 1})
    assert await storage.get_state(key) == "S:one" and await storage.get_value(key, "a") == 1
    await storage.set_state(key, None)
    await storage.set_data(key, {})
    assert storage.storage == {}


async def test_group_messages_do_not_create_fsm_records(dp, bot):  # noqa: F811
    for user in range(50):
        await dp.feed_update(bot, group_message(-106001, user=SimpleUser(100000 + user), text="привет"))
    assert len(dp.fsm.storage.storage) == 0
