"""Защита секретов: обёртка для ключей, маскировка в логах и текстах ошибок.

Правило проекта: ключ API, токен бота или токен доступа не должен попасть
ни в журнал, ни в текст исключения, ни в ответ пользователю. Для этого:

* ключи хранятся в :class:`Secret` — его ``repr``/``str`` не раскрывают значение;
* каждый секрет регистрируется в :data:`REGISTRY`, и фильтр логов
  :class:`RedactingFilter` заменяет его на ``***`` во всех сообщениях,
  включая трассировки исключений;
* второй слой — шаблоны известных форматов (токен Telegram, заголовки
  ``Bearer``/``Api-Key``/``Basic``, ключи вида ``sk-...``) на случай, если
  секрет не был зарегистрирован (например, пришёл в URL сторонней библиотеки).
"""

from __future__ import annotations

import logging
import re
import sys
import threading
from urllib.parse import urlsplit, urlunsplit

MASK = "***"

# Секреты короче этого не регистрируем: замена коротких строк портила бы обычный текст.
_MIN_SECRET_LEN = 6

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Токен Telegram-бота: 123456789:AA...; встречается в URL вида /bot<token>/method.
    re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}"),
    # Заголовки авторизации в текстах ошибок и отладочных дампах.
    re.compile(r"(?i)(\b(?:bearer|api-key|basic)\s+)[A-Za-z0-9._~+/=-]{8,}"),
    # Ключи в стиле OpenAI/OpenRouter.
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
)


class Secret:
    """Строка-секрет. Значение достаётся только явным вызовом :meth:`get`."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None) -> None:
        self._value = value or ""

    def get(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret('***')" if self._value else "Secret('')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("Secret", self._value))


class SecretRegistry:
    """Набор значений, которые нельзя выводить ни в каком виде."""

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = threading.Lock()

    def add(self, value: str | Secret | None) -> None:
        raw = value.get() if isinstance(value, Secret) else value
        if raw and len(raw) >= _MIN_SECRET_LEN:
            with self._lock:
                self._values.add(raw)

    def discard(self, value: str | None) -> None:
        if value:
            with self._lock:
                self._values.discard(value)

    def redact(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            if value in text:
                text = text.replace(value, MASK)
        for pattern in _PATTERNS:
            text = pattern.sub(_mask_match, text)
        return text


def _mask_match(match: re.Match[str]) -> str:
    # Для шаблона заголовка сохраняем само слово (Bearer/Api-Key), маскируем значение.
    if match.lastindex:
        return match.group(1) + MASK
    return MASK


REGISTRY = SecretRegistry()


def redact(text: str) -> str:
    """Убрать из текста все известные секреты."""
    return REGISTRY.redact(text)


def safe_url(url: str) -> str:
    """URL для показа в логах: без логина/пароля, параметров запроса и якоря."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<некорректный URL>"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


class RedactingFilter(logging.Filter):
    """Фильтр для обработчиков логов: маскирует секреты в тексте и трассировке."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - кривой формат не должен ронять логирование
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def setup_logging(level: str = "INFO") -> None:
    """Настроить корневой логгер: вывод в stderr и обязательная маскировка секретов."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level.upper())
    # httpx на уровне INFO пишет каждый запрос — это шум для журнала бота.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
