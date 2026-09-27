"""Логирование без секретов.

Токены маскируются в итоговой строке лога — включая аргументы сообщения и
текст трейсбека. Дополнительно по шаблону маскируется всё, что похоже на токен
Telegram (`123456:AA...`), даже если такого токена нет в настройках: он может
оказаться, например, в URL запроса внутри текста исключения.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable

MASK = "***"
_MIN_SECRET_LENGTH = 6
# Токен Telegram: <id бота>:<35+ символов>. Граница слева — не цифра, чтобы ловить и /bot123:ABC.
TELEGRAM_TOKEN_RE = re.compile(r"(?<![0-9])\d{5,}:[A-Za-z0-9_-]{30,}")


def mask_secrets(text: str, secrets: Iterable[str] = ()) -> str:
    """Заменить известные секреты и токены Telegram на `***`."""
    for secret in sorted((s for s in secrets if s and len(s) >= _MIN_SECRET_LENGTH), key=len, reverse=True):
        text = text.replace(secret, MASK)
    return TELEGRAM_TOKEN_RE.sub(MASK, text)


class SecretMaskingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: Iterable[str] = (), datefmt: str | None = None) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._secrets = tuple(secrets)

    def format(self, record: logging.LogRecord) -> str:
        return mask_secrets(super().format(record), self._secrets)


LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: str = "INFO", secrets: Iterable[str] = ()) -> None:
    """Настроить корневой логгер: вывод в stderr (journald/Docker заберут его сами)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SecretMaskingFormatter(LOG_FORMAT, secrets))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    # Сетевые библиотеки на DEBUG печатают URL запросов — держим их на INFO и выше.
    for noisy in ("aiohttp.access", "aiohttp.client", "aiogram.event"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, root.level))
