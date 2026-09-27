"""Запуск: без токенов — понятная ошибка; неверный токен — транспорт выключается, а не крутится."""

from __future__ import annotations

from pathlib import Path

import pytest

from leads_bot.app import _supervise, run_bot
from leads_bot.config import BotConfig, ConfigError, Settings
from leads_bot.transports.max_api import MaxAuthError


async def test_no_tokens(config: BotConfig, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="запускать нечего"):
        await run_bot(Settings(database_path=tmp_path / "db.sqlite3"), config)


async def test_supervisor_stops_on_bad_token(caplog: pytest.LogCaptureFixture) -> None:
    calls = 0

    async def run() -> None:
        nonlocal calls
        calls += 1
        raise MaxAuthError(401, "verify.token", "Invalid access_token")

    await _supervise("max", run, first_delay=0)
    assert calls == 1
    assert "токен не принят" in caplog.text


async def test_supervisor_restarts_after_crash() -> None:
    calls = 0

    async def run() -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("network down")
        raise MaxAuthError(401, None, "stop")

    await _supervise("max", run, first_delay=0)
    assert calls == 3
