"""Хранение в SQLite, статистика, плановая очистка."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from leads_bot.config import BotConfig
from leads_bot.core.models import Channel, Dialog, LeadDraft, Step, UserRef
from leads_bot.core.service import LeadService, Payload
from leads_bot.core.stats import collect_stats, format_stats
from leads_bot.core.storage import Storage
from tests.helpers import FakeClock

UTC = timezone.utc


def draft(created_at: datetime, *, channel: Channel = Channel.TELEGRAM, user_id: int = 1, service: str = "Электрика") -> LeadDraft:
    return LeadDraft(
        created_at=created_at,
        channel=channel,
        user_id=user_id,
        username="ivan",
        display_name="Иван",
        service_id="electric",
        service_title=service,
        name="Иван",
        phone="+79991234567",
        comment="Комментарий",
        consent_at=created_at - timedelta(minutes=1),
        policy_url="https://example.com/privacy",
        policy_version="v1",
    )


def test_lead_roundtrip_and_persistence(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "leads.sqlite3"  # папка создаётся сама
    created = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)
    with Storage(path) as storage:
        lead = storage.add_lead(draft(created))
        assert lead.id == 1 and not lead.admin_notified
        storage.mark_notified(lead.id)

    with Storage(path) as reopened:
        [loaded] = reopened.list_leads()
        assert loaded.created_at == created and loaded.created_at.tzinfo is not None
        assert loaded.consent_at == created - timedelta(minutes=1)
        assert (loaded.channel, loaded.phone, loaded.policy_version) == (Channel.TELEGRAM, "+79991234567", "v1")
        assert loaded.admin_notified


def test_naive_datetime_rejected(storage: Storage) -> None:
    with pytest.raises(ValueError):
        storage.add_lead(draft(datetime(2026, 9, 27, 9, 0)))


def test_dialog_save_load_delete(storage: Storage) -> None:
    now = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)
    storage.save_dialog(Dialog(Channel.MAX, 7, Step.NAME, now, {"service_title": "Санузел и плитка"}))
    loaded = storage.get_dialog(Channel.MAX, 7)
    assert loaded is not None and loaded.step is Step.NAME and loaded.data == {"service_title": "Санузел и плитка"}
    assert storage.get_dialog(Channel.TELEGRAM, 7) is None
    loaded.step = Step.PHONE
    storage.save_dialog(loaded)
    assert storage.get_dialog(Channel.MAX, 7).step is Step.PHONE  # type: ignore[union-attr]
    storage.delete_dialog(Channel.MAX, 7)
    assert storage.get_dialog(Channel.MAX, 7) is None


def test_delete_user_data_is_scoped(storage: Storage) -> None:
    now = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)
    storage.add_lead(draft(now, channel=Channel.TELEGRAM, user_id=5))
    storage.add_lead(draft(now, channel=Channel.MAX, user_id=5))
    storage.add_lead(draft(now, channel=Channel.TELEGRAM, user_id=6))
    assert storage.delete_user_data(Channel.TELEGRAM, 5) == 1
    assert sorted((lead.channel.value, lead.user_id) for lead in storage.list_leads()) == [("max", 5), ("telegram", 6)]


def test_kv(storage: Storage) -> None:
    assert storage.get_value("max.updates_marker") is None
    storage.set_value("max.updates_marker", "10")
    storage.set_value("max.updates_marker", "11")
    assert storage.get_value("max.updates_marker") == "11"


def test_refuses_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "leads.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(RuntimeError, match="более новой версией"):
        Storage(path)


def test_stats_day_week_total(storage: Storage, config: BotConfig) -> None:
    now = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)  # 12:00 МСК
    storage.add_lead(draft(datetime(2026, 9, 26, 21, 1, tzinfo=UTC), service="Электрика"))  # 00:01 МСК 27.09 — сегодня
    storage.add_lead(draft(datetime(2026, 9, 26, 20, 59, tzinfo=UTC), service="Электрика"))  # 23:59 МСК 26.09 — вчера
    storage.add_lead(draft(now - timedelta(days=3), channel=Channel.MAX, service="Дизайн-проект"))
    storage.add_lead(draft(now - timedelta(days=8), service="Другое"))  # старше недели

    stats = collect_stats(storage, now, config.tz)
    assert (stats.today, stats.week, stats.total) == (1, 3, 4)
    assert stats.by_service_week == [("Электрика", 2), ("Дизайн-проект", 1)]
    assert stats.by_channel_total == {Channel.TELEGRAM: 3, Channel.MAX: 1}

    text = format_stats(stats, config.tz)
    assert "Сегодня: <b>1</b>" in text and "За 7 дней: <b>3</b>" in text and "Всего: <b>4</b>" in text
    assert "Telegram: 3" in text and "MAX: 1" in text
    assert "27.09.2026 12:00" in text


def test_stats_empty(storage: Storage, config: BotConfig) -> None:
    stats = collect_stats(storage, datetime(2026, 9, 27, 9, 0, tzinfo=UTC), config.tz)
    assert (stats.today, stats.week, stats.total) == (0, 0, 0)
    assert "Всего: <b>0</b>" in format_stats(stats, config.tz)


async def test_maintenance_purges_abandoned_dialogs_and_old_leads(
    config: BotConfig, storage: Storage, clock: FakeClock
) -> None:
    service = LeadService(config.model_copy(update={"retention_days": 30}), storage, clock=clock)
    storage.add_lead(draft(clock() - timedelta(days=31)))
    storage.add_lead(draft(clock() - timedelta(days=28)))
    abandoned = UserRef(channel=Channel.TELEGRAM, user_id=1)
    await service.start(abandoned)
    await service.handle_button(abandoned, Payload.CONSENT_YES)
    clock.advance(hours=config.dialog_ttl_hours + 1)
    fresh = UserRef(channel=Channel.TELEGRAM, user_id=2)
    await service.start(fresh)

    service.run_maintenance()
    assert storage.count_dialogs() == 1
    assert storage.get_dialog(Channel.TELEGRAM, 2) is not None
    assert storage.count_leads() == 1


async def test_retention_zero_keeps_leads(service: LeadService, storage: Storage, clock: FakeClock) -> None:
    storage.add_lead(draft(clock() - timedelta(days=3650)))
    service.run_maintenance()
    assert storage.count_leads() == 1
