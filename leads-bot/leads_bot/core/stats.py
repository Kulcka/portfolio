"""Статистика заявок для команды /stats."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from .models import Channel
from .storage import Storage


@dataclass(frozen=True)
class LeadStats:
    today: int
    week: int
    total: int
    by_service_week: list[tuple[str, int]]
    by_channel_total: dict[Channel, int]
    generated_at: datetime


def start_of_local_day(now: datetime, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def collect_stats(storage: Storage, now: datetime, tz: ZoneInfo) -> LeadStats:
    """«Сегодня» — с полуночи по часовому поясу из конфига, «за 7 дней» — скользящие 7×24 часа."""
    week_start = now - timedelta(days=7)
    return LeadStats(
        today=storage.count_leads(since=start_of_local_day(now, tz)),
        week=storage.count_leads(since=week_start),
        total=storage.count_leads(),
        by_service_week=storage.count_by_service(since=week_start),
        by_channel_total=storage.count_by_channel(),
        generated_at=now,
    )


def format_stats(stats: LeadStats, tz: ZoneInfo) -> str:
    lines = [
        "<b>Заявки</b>",
        f"Сегодня: <b>{stats.today}</b>",
        f"За 7 дней: <b>{stats.week}</b>",
        f"Всего: <b>{stats.total}</b>",
    ]
    if stats.by_service_week:
        lines += ["", "<b>По услугам за 7 дней</b>"]
        lines += [f"{escape(title)}: {count}" for title, count in stats.by_service_week]
    if stats.by_channel_total:
        lines += ["", "<b>По каналам, всего</b>"]
        lines += [f"{channel.title}: {count}" for channel, count in sorted(stats.by_channel_total.items())]
    lines += ["", f"<i>На {stats.generated_at.astimezone(tz).strftime('%d.%m.%Y %H:%M')}</i>"]
    return "\n".join(lines)
