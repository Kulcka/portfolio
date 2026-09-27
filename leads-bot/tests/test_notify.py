"""Уведомление администратору."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from leads_bot.config import BotConfig, Notifications
from leads_bot.core.models import Channel, Lead, UserRef
from leads_bot.core.notify import format_admin_notification
from leads_bot.core.service import LeadService
from leads_bot.core.storage import Storage
from tests.helpers import FakeClock, RecordingNotifier, fill_lead


def make_lead(**overrides: object) -> Lead:
    moment = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
    fields: dict[str, object] = dict(
        id=12,
        created_at=moment,
        channel=Channel.TELEGRAM,
        user_id=1001,
        username="ivan",
        display_name="Иван Петров",
        service_id="electric",
        service_title="Электрика",
        name="Иван",
        phone="+79991234567",
        comment="Щиток & розетки <срочно>",
        consent_at=moment,
        policy_url="https://example.com/privacy",
        policy_version="",
    )
    fields.update(overrides)
    return Lead(**fields)  # type: ignore[arg-type]


def test_format_telegram(config: BotConfig) -> None:
    text = format_admin_notification(make_lead(), config.tz)
    assert text.startswith("<b>Новая заявка №12</b>")
    assert "<b>Услуга:</b> Электрика" in text
    assert "<b>Имя:</b> Иван" in text
    assert "<b>Телефон:</b> +7 999 123-45-67" in text
    assert "<b>Комментарий:</b> Щиток &amp; розетки &lt;срочно&gt;" in text
    assert '<a href="tg://user?id=1001">Иван Петров (@ivan)</a>' in text
    assert "<b>Время:</b> 27.09.2026 12:00" in text


def test_format_max_without_comment(config: BotConfig) -> None:
    text = format_admin_notification(
        make_lead(channel=Channel.MAX, user_id=555, username=None, comment=""), config.tz
    )
    assert '<b>Откуда:</b> MAX, <a href="max://user/555">Иван Петров</a>' in text
    assert "Комментарий" not in text


def test_format_hidden_phone(config: BotConfig) -> None:
    text = format_admin_notification(make_lead(), config.tz, hide_phone=True)
    assert "+7 999 ***-**-67" in text
    assert "123-45-67" not in text and "+79991234567" not in text
    assert "/export" in text


async def test_notification_uses_mask_setting(
    config: BotConfig, storage: Storage, clock: FakeClock, user: UserRef
) -> None:
    notifier = RecordingNotifier()
    masked = config.model_copy(update={"notifications": Notifications(mask_phone=True)})
    await fill_lead(LeadService(masked, storage, notifiers=[notifier], clock=clock), user)
    assert "***-**-67" in notifier.messages[0]
    assert storage.list_leads()[0].phone == "+79991234567"  # в базе номер полный


async def test_failed_notifier_does_not_lose_lead(
    config: BotConfig, storage: Storage, clock: FakeClock, user: UserRef, caplog: pytest.LogCaptureFixture
) -> None:
    broken = RecordingNotifier(fail=True, name="broken")
    working = RecordingNotifier(name="working")
    service = LeadService(config, storage, notifiers=[broken, working], clock=clock)
    [accepted] = await fill_lead(service, user)
    assert "№1" in accepted.text  # клиент получил подтверждение
    assert len(working.messages) == 1
    assert storage.list_leads()[0].admin_notified
    assert "Не удалось отправить уведомление о заявке №1 через broken" in caplog.text


async def test_all_notifiers_failed(config: BotConfig, storage: Storage, clock: FakeClock, user: UserRef) -> None:
    service = LeadService(config, storage, notifiers=[RecordingNotifier(fail=True)], clock=clock)
    [accepted] = await fill_lead(service, user)
    assert "№1" in accepted.text
    [lead] = storage.list_leads()
    assert not lead.admin_notified  # видно в выгрузке: «Уведомление отправлено — нет»


async def test_no_notifiers_logs_warning(
    config: BotConfig, storage: Storage, clock: FakeClock, user: UserRef, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    await fill_lead(LeadService(config, storage, clock=clock), user)
    assert storage.count_leads() == 1
    assert "админ-чат не настроен" in caplog.text


async def test_logs_have_no_personal_data(
    service: LeadService, user: UserRef, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await fill_lead(service, user, name="Иван", phone="+7 999 123-45-67", comment="секретный комментарий")
    await service.forget(user)
    assert "Заявка №1 принята" in caplog.text
    for personal in ("Иван", "999", "секретный"):
        assert personal not in caplog.text
