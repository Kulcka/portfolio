"""Общие помощники тестов: подменённые часы, уведомитель, заполнение заявки."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from leads_bot.core.models import Reply, UserRef
from leads_bot.core.service import LeadService, Payload

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class RecordingNotifier:
    def __init__(self, *, fail: bool = False, name: str = "test") -> None:
        self.name = name
        self.fail = fail
        self.messages: list[str] = []

    async def notify_admin(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("admin chat unavailable")
        self.messages.append(text)


async def fill_lead(
    service: LeadService,
    user: UserRef,
    *,
    service_id: str = "cosmetic",
    name: str = "Иван",
    phone: str = "+7 999 123-45-67",
    comment: str | None = "Позвоните после 18:00",
    send: bool = True,
) -> list[Reply]:
    """Пройти сценарий целиком; вернуть ответы на последнем шаге."""
    await service.start(user)
    await service.handle_button(user, Payload.CONSENT_YES)
    await service.handle_button(user, Payload.SERVICE_PREFIX + service_id)
    await service.handle_text(user, name)
    await service.handle_text(user, phone)
    if comment is None:
        replies = await service.handle_button(user, Payload.SKIP_COMMENT)
    else:
        replies = await service.handle_text(user, comment)
    if send:
        replies = await service.handle_button(user, Payload.SEND)
    return replies
