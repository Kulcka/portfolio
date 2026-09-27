"""Уведомления в Telegram: сводка изменений и сообщения об ошибках.

Токен бота и chat_id берутся из ``.env`` (``TELEGRAM_BOT_TOKEN``,
``TELEGRAM_CHAT_ID``). Токен входит в адрес API, поэтому все тексты ошибок
проходят через ``redact`` — в логи и исключения токен не попадает.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import requests

from site_parser.config import URL_COLUMN, SiteConfig
from site_parser.diff import DiffResult
from site_parser.redact import redact, register_secrets
from site_parser.report import fmt_percent, fmt_value

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class NotifyError(Exception):
    """Уведомление не отправлено. Текст не содержит секретов."""


def _e(text: Any) -> str:
    return html.escape(str(text), quote=False)


def _link(title: str, url: str) -> str:
    if url.startswith(("http://", "https://")):
        return f'<a href="{html.escape(url, quote=True)}">{_e(title)}</a>'
    return _e(title)


def _section(title: str, lines: list[str], max_lines: int) -> list[str]:
    if not lines:
        return []
    shown = lines[:max_lines]
    out = ["", f"<b>{_e(title)}</b>", *shown]
    if len(lines) > len(shown):
        out.append(f"…и ещё {len(lines) - len(shown)}")
    return out


def _build(diff: DiffResult, config: SiteConfig, finished_at: datetime, errors_count: int, max_lines: int) -> str:
    title_field = config.title_field()
    price_field = config.price_field()
    bools = config.output.bool_values

    def title_of(item: dict[str, Any]) -> str:
        value = item.get(title_field) if title_field else None
        return str(value) if value not in (None, "") else str(item.get(URL_COLUMN, ""))

    lines = [f"<b>{_e(config.title)}</b> — {finished_at:%d.%m.%Y %H:%M}"]
    if diff.is_first_run:
        lines.append(f"Первый прогон: собрано записей {diff.current_count}.")
        lines.append("Изменения появятся со следующего прогона.")
    else:
        lines.append(f"Записей: {diff.current_count} (было {diff.previous_count})")
        c = diff.counts()
        summary = f"Новые: {c['new']} · Цена: {c['price']} · Пропали: {c['gone']}"
        if config.changes.track:
            summary += f" · Другое: {c['other']}"
        lines.append(summary)
        if not diff.has_changes:
            lines.append("Изменений нет.")

        price_lines = [
            f"• {_link(ch.title, ch.url)}: {_e(fmt_value(ch.old))} → {_e(fmt_value(ch.new))}"
            f" ({_e(fmt_percent(ch.delta_percent))})"
            for ch in diff.price_changes
        ]
        new_lines = [
            f"• {_link(title_of(item), str(item.get(URL_COLUMN, '')))}"
            + (f" — {_e(fmt_value(item.get(price_field)))}" if price_field else "")
            for item in diff.new
        ]
        gone_lines = [
            f"• {_link(title_of(item), str(item.get(URL_COLUMN, '')))}"
            + (f" — {_e(fmt_value(item.get(price_field)))}" if price_field else "")
            for item in diff.gone
        ]
        other_lines = [
            f"• {_link(fc.title, fc.url)}: {_e(config.label_for(fc.field))} {_e(fmt_value(fc.old, bools))}"
            f" → {_e(fmt_value(fc.new, bools))}"
            for fc in diff.field_changes
        ]
        lines += _section("Изменилась цена", price_lines, max_lines)
        lines += _section("Новые", new_lines, max_lines)
        lines += _section("Пропали", gone_lines, max_lines)
        lines += _section("Другие изменения", other_lines, max_lines)
        if diff.gone_suppressed_reason:
            lines += ["", f"«Пропали» не считаем: {_e(diff.gone_suppressed_reason)}."]
    if errors_count:
        lines += ["", f"Ошибок загрузки: {errors_count} — прогон неполный, подробности в логе."]
    return "\n".join(lines)


def format_changes_message(
    diff: DiffResult,
    config: SiteConfig,
    *,
    finished_at: datetime,
    errors_count: int = 0,
    max_lines: int = 10,
) -> str:
    """Текст уведомления (HTML-разметка Telegram), не длиннее 4096 символов."""
    text = _build(diff, config, finished_at, errors_count, max_lines)
    while len(text) > TELEGRAM_LIMIT and max_lines > 1:
        max_lines //= 2
        text = _build(diff, config, finished_at, errors_count, max_lines)
    if len(text) > TELEGRAM_LIMIT:
        text = _build(diff, config, finished_at, errors_count, 0)
    return text[:TELEGRAM_LIMIT]


def format_error_message(title: str, error: str, when: datetime) -> str:
    """Короткое сообщение о сбое прогона по расписанию."""
    return f"<b>{_e(title)}</b> — {when:%d.%m.%Y %H:%M}\nПрогон не выполнен: {_e(redact(error))[:3500]}"


PostFunc = Callable[..., Any]


class TelegramNotifier:
    """Отправка сообщений ботом Telegram."""

    def __init__(self, token: str, chat_id: str, *, session: Any | None = None, timeout: float = 15.0) -> None:
        if not token or not chat_id:
            raise ValueError("нужны токен бота и chat_id")
        register_secrets([token])
        self._token = token
        self.chat_id = chat_id
        self._session = session if session is not None else requests.Session()
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"TelegramNotifier(chat_id={self.chat_id!r}, token=***)"

    def send(self, text: str) -> None:
        url = API_URL.format(token=self._token)
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        try:
            response = self._session.post(url, data=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise NotifyError(redact(f"Telegram: сбой сети ({type(exc).__name__}): {exc}", [self._token])) from None
        if response.status_code != 200:
            try:
                description = response.json().get("description", "")
            except ValueError:
                description = ""
            raise NotifyError(redact(f"Telegram ответил HTTP {response.status_code}: {description}", [self._token]))
        log.info("Уведомление отправлено в Telegram")
