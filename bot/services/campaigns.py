"""Проверки перед запуском рассылки и состояние её чатов."""

from __future__ import annotations

from collections.abc import Sequence
from zoneinfo import ZoneInfo

from bot.db.models import Campaign, Post
from bot.db.repo import Target
from bot.services.schedule_utils import next_slot


def schedule_problems(campaign: Campaign, posts: Sequence[Post], tz: ZoneInfo, now: int) -> list[str]:
    problems = []
    if not posts:
        problems.append("Сначала добавьте хотя бы один пост")
    if not campaign.times:
        problems.append("Сначала задайте время публикаций")
    if not campaign.weekdays:
        problems.append("Выберите хотя бы один день недели")
    if (
        campaign.times
        and campaign.weekdays
        and next_slot(campaign.times, campaign.weekdays, tz, now, campaign.start_date, campaign.end_date) is None
    ):
        problems.append("Период публикаций уже закончился — измените даты в расписании")
    return problems


def target_problem(target: Target) -> str | None:
    """Почему бот сейчас не публикует в этот чат (None — публикует)."""
    chat, link = target.chat, target.link
    if chat.status == "left":
        return "бота нет в чате"
    if chat.status == "pending":
        return "чат не подтверждён"
    if not chat.can_post:
        return "нет права публиковать"
    if link.paused:
        return "пауза после ошибок"
    return None


def targets_problems(targets: Sequence[Target], *, on_start: bool = False) -> list[str]:
    """on_start — проверка перед запуском: паузу после ошибок запуск снимает, её не считаем помехой."""
    if not targets:
        return ["Отметьте чаты, в которые публиковать, — кнопка «💬 Чаты»"]
    if not any(t.chat.status == "active" and t.chat.can_post and (on_start or not t.link.paused) for t in targets):
        return ["Ни в одном из отмеченных чатов бот сейчас не может публиковать — откройте «💬 Чаты»"]
    return []


def readiness_problems(
    campaign: Campaign, posts: Sequence[Post], targets: Sequence[Target], tz: ZoneInfo, now: int
) -> list[str]:
    """Что мешает запустить рассылку (запуск снимает паузу после ошибок во всех её чатах)."""
    return targets_problems(targets, on_start=True) + schedule_problems(campaign, posts, tz, now)
