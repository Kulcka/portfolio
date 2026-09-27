"""Вымышленные заявки для показа /stats и /export в демо-базе.

Имена вымышленные, номера — из диапазона +7 900 000-xx-xx, в комментарии
пометка «демо». Только для отдельной демо-базы, не для рабочей.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from .config import BotConfig
from .core.models import Channel, LeadDraft
from .core.storage import Storage

_NAMES = ("Анна", "Иван", "Мария", "Сергей", "Ольга", "Дмитрий", "Екатерина", "Алексей", "Наталья", "Павел")
_COMMENTS = (
    "Квартира 54 м², вторичка, нужен замер",
    "Удобно звонить после 18:00",
    "Санузел под ключ, плитка уже куплена",
    "Хотим до конца месяца",
    "",
    "Нужна смета, пришлите примеры работ",
)


def seed_demo_leads(storage: Storage, config: BotConfig, *, count: int, now: datetime, seed: int = 7) -> int:
    rng = random.Random(seed)
    for index in range(count):
        created = now - timedelta(days=rng.uniform(0, 14), minutes=rng.randint(0, 600))
        service = rng.choice(config.services)
        channel = rng.choice((Channel.TELEGRAM, Channel.TELEGRAM, Channel.MAX))
        name = rng.choice(_NAMES)
        comment = rng.choice(_COMMENTS)
        storage.add_lead(
            LeadDraft(
                created_at=created,
                channel=channel,
                user_id=100000 + index,
                username=f"demo_client_{index}" if channel is Channel.TELEGRAM else None,
                display_name=name,
                service_id=service.id,
                service_title=service.title,
                name=name,
                phone=f"+7900000{index:04d}",
                comment=(comment + " (демо)").strip() if comment else "(демо)",
                consent_at=created - timedelta(minutes=2),
                policy_url=config.privacy.policy_url,
                policy_version=config.privacy.policy_version,
            )
        )
    return count
