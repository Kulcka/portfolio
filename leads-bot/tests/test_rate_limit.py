"""Антиспам: не больше N заявок с одного пользователя за час."""

from __future__ import annotations

from datetime import datetime, timezone

from leads_bot.config import Antispam, BotConfig
from leads_bot.core.models import Channel, LeadDraft, UserRef
from leads_bot.core.service import LeadService, Payload
from leads_bot.core.storage import Storage
from tests.helpers import FakeClock, fill_lead


async def test_limit_blocks_new_lead_and_says_when(
    service: LeadService, storage: Storage, user: UserRef, clock: FakeClock, config: BotConfig
) -> None:
    assert config.antispam.max_leads_per_hour == 3
    for _ in range(3):
        await fill_lead(service, user)
        clock.advance(minutes=1)
    assert storage.count_leads() == 3

    # Первая заявка была в 09:00, сейчас 09:03 — ждать до 10:00, 57 минут.
    [blocked] = await service.start(user)
    assert blocked.text == config.texts.rate_limited.format(count=3, minutes=57, company_phone=config.company.phone)
    assert storage.get_dialog(user.channel, user.user_id) is None

    # Другой пользователь не затронут.
    other = UserRef(channel=Channel.TELEGRAM, user_id=2002)
    [consent] = await service.start(other)
    assert consent.buttons

    # Через час после первой заявки лимит освобождается.
    clock.advance(minutes=57)
    [consent] = await service.start(user)
    assert consent.buttons and consent.buttons[1].value == Payload.CONSENT_YES


async def test_limit_is_rechecked_on_submit(
    config: BotConfig, storage: Storage, user: UserRef, clock: FakeClock
) -> None:
    strict = config.model_copy(update={"antispam": Antispam(max_leads_per_hour=1)})
    service = LeadService(strict, storage, clock=clock)
    await fill_lead(service, user, send=False)  # на старте заявок не было — пустили

    # Пока человек заполнял форму, заявка от него уже появилась (например, двойная отправка).
    storage.add_lead(
        LeadDraft(
            created_at=clock(),
            channel=user.channel,
            user_id=user.user_id,
            username=None,
            display_name=None,
            service_id="other",
            service_title="Другое",
            name="Иван",
            phone="+79991234567",
            comment="",
            consent_at=clock(),
            policy_url=config.privacy.policy_url,
            policy_version="",
        )
    )
    [reply] = await service.handle_button(user, Payload.SEND)
    assert reply.text.startswith("Заявок от вас за последний час: 1")
    assert storage.count_leads() == 1


async def test_limit_counts_only_last_hour(
    service: LeadService, storage: Storage, user: UserRef, clock: FakeClock
) -> None:
    for _ in range(3):
        await fill_lead(service, user)
        clock.advance(minutes=30)
    # Сейчас 10:30: за последние 60 минут только заявка 10:00 (09:30 ровно час назад — уже вне окна).
    assert clock() == datetime(2026, 9, 27, 10, 30, tzinfo=timezone.utc)
    [consent] = await service.start(user)
    assert consent.buttons
