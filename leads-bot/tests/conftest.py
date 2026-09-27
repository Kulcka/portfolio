from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from leads_bot.config import BotConfig, load_bot_config
from leads_bot.core.models import Channel, UserRef
from leads_bot.core.service import LeadService
from leads_bot.core.storage import Storage
from tests.helpers import CONFIG_PATH, FakeClock, RecordingNotifier

# 27.09.2026 09:00 UTC = 12:00 по Москве
START = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def config() -> BotConfig:
    return load_bot_config(CONFIG_PATH)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[Storage]:
    with Storage(tmp_path / "leads.sqlite3") as db:
        yield db


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def service(config: BotConfig, storage: Storage, notifier: RecordingNotifier, clock: FakeClock) -> LeadService:
    return LeadService(config, storage, notifiers=[notifier], clock=clock)


@pytest.fixture
def user() -> UserRef:
    return UserRef(channel=Channel.TELEGRAM, user_id=1001, username="ivan", display_name="Иван Петров")
