"""Маскировка секретов в логах и текстах ошибок.

Секреты (токен Telegram и т.п.) регистрируются один раз при старте, после чего
``RedactingFormatter`` вычищает их из любой строки лога, включая трейсбеки
исключений. Дополнительно по шаблону маскируются токены ботов Telegram — на
случай, если токен попал в текст из стороннего источника (например, из URL в
сообщении ``requests``).
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable

MASK = "***"

# Токен бота Telegram: <числовой id>:<35 символов>. В адресе API он идёт сразу
# после «bot» (…/bot123456:AA…/sendMessage), поэтому без \b в начале.
_TELEGRAM_TOKEN_RE = re.compile(r"(?<![0-9])\d{6,12}:[A-Za-z0-9_-]{30,}")

_lock = threading.Lock()
_secrets: set[str] = set()


def register_secrets(values: Iterable[str | None]) -> None:
    """Запомнить значения, которые нельзя выводить в логи."""
    with _lock:
        for value in values:
            # Слишком короткие строки не маскируем: иначе «***» появится
            # посреди обычного текста.
            if value and len(value) >= 6:
                _secrets.add(value)


def clear_secrets() -> None:
    """Забыть все секреты (нужно тестам)."""
    with _lock:
        _secrets.clear()


def redact(text: str, extra: Iterable[str | None] = ()) -> str:
    """Вернуть ``text`` с замаскированными секретами."""
    with _lock:
        known = set(_secrets)
    known.update(v for v in extra if v and len(v) >= 6)
    # Сначала длинные: если один секрет содержит другой, маскируем целиком.
    for secret in sorted(known, key=len, reverse=True):
        text = text.replace(secret, MASK)
    return _TELEGRAM_TOKEN_RE.sub(MASK, text)


class RedactingFormatter(logging.Formatter):
    """Форматтер логов, который маскирует секреты в готовой строке."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
