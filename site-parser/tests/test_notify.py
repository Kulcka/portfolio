"""Форматирование и отправка уведомлений в Telegram; секреты не попадают в логи и ошибки."""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
import requests

from site_parser.config import SiteConfig
from site_parser.diff import DiffResult, FieldChange, PriceChange
from site_parser.notify import (
    TELEGRAM_LIMIT,
    NotifyError,
    TelegramNotifier,
    format_changes_message,
    format_error_message,
)
from site_parser.redact import MASK, RedactingFormatter, redact, register_secrets
from site_parser.storage import RunInfo
from tests.conftest import FakeResponse, FakeSession

WHEN = datetime(2026, 9, 27, 14, 5)
TOKEN = "123456789:AAH-fake-token-for-tests-only-0123456789"
PREV = RunInfo(1, "books_toscrape", WHEN, WHEN, True, 3, 5)


def _diff(**kwargs: object) -> DiffResult:
    base: dict[str, object] = {"previous_run": PREV, "current_count": 3, "previous_count": 3}
    base.update(kwargs)
    return DiffResult(**base)  # type: ignore[arg-type]


def test_message_with_all_sections(books_config: SiteConfig) -> None:
    diff = _diff(
        new=[{"title": "Новая <книга>", "price": 12.5, "url": "https://books.toscrape.com/n"}],
        gone=[{"title": "Старая & редкая", "price": 3.0, "url": "https://books.toscrape.com/g"}],
        price_changes=[PriceChange("u1", "A Light in the Attic", "https://books.toscrape.com/a?x=1&y=2", 51.77, 45.0)],
        field_changes=[FieldChange("u2", "Tipping", "https://books.toscrape.com/t", "in_stock", True, False)],
    )
    text = format_changes_message(diff, books_config, finished_at=WHEN)
    assert text.splitlines()[0] == "<b>Books to Scrape</b> — 27.09.2026 14:05"
    assert "Новые: 1 · Цена: 1 · Пропали: 1 · Другое: 1" in text
    assert '<a href="https://books.toscrape.com/a?x=1&amp;y=2">A Light in the Attic</a>: 51.77 → 45.00 (−13.1%)' in text
    assert "Новая &lt;книга&gt;</a> — 12.50" in text  # HTML экранирован
    assert "Старая &amp; редкая</a> — 3.00" in text
    assert "В наличии да → нет" in text
    order = [
        text.index(s) for s in ("<b>Изменилась цена</b>", "<b>Новые</b>", "<b>Пропали</b>", "<b>Другие изменения</b>")
    ]
    assert order == sorted(order)


def test_first_run_and_no_changes(books_config: SiteConfig) -> None:
    first = format_changes_message(DiffResult(previous_run=None, current_count=40), books_config, finished_at=WHEN)
    assert "Первый прогон: собрано записей 40." in first
    same = format_changes_message(_diff(), books_config, finished_at=WHEN)
    assert "Изменений нет." in same


def test_long_lists_are_truncated_to_telegram_limit(books_config: SiteConfig) -> None:
    new = [
        {"title": f"Книга номер {n} " + "x" * 80, "price": float(n), "url": f"https://books.toscrape.com/{n}"}
        for n in range(300)
    ]
    text = format_changes_message(_diff(new=new, current_count=300), books_config, finished_at=WHEN, max_lines=10)
    assert "…и ещё 290" in text
    huge = format_changes_message(_diff(new=new * 20), books_config, finished_at=WHEN, max_lines=200)
    assert len(huge) <= TELEGRAM_LIMIT


def test_errors_and_suppressed_gone_are_mentioned(books_config: SiteConfig) -> None:
    diff = _diff(gone_suppressed_reason="прогон прошёл с ошибками загрузки")
    text = format_changes_message(diff, books_config, finished_at=WHEN, errors_count=2)
    assert "Ошибок загрузки: 2" in text and "«Пропали» не считаем" in text


def test_send_posts_html_message() -> None:
    session = FakeSession()
    TelegramNotifier(TOKEN, "-100500", session=session).send("<b>привет</b>")
    url, kwargs = session.posts[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert kwargs["data"]["chat_id"] == "-100500"
    assert kwargs["data"]["parse_mode"] == "HTML"
    assert kwargs["data"]["text"] == "<b>привет</b>"


def test_token_never_leaks_in_errors() -> None:
    session = FakeSession()
    session.post_response = requests.ConnectionError(
        f"HTTPSConnectionPool(host='api.telegram.org'): Max retries exceeded with url: /bot{TOKEN}/sendMessage"
    )
    notifier = TelegramNotifier(TOKEN, "1", session=session)
    with pytest.raises(NotifyError) as info:
        notifier.send("x")
    assert TOKEN not in str(info.value) and MASK in str(info.value)
    assert TOKEN not in repr(notifier)

    session.post_response = FakeResponse(401, b"", _json={"ok": False, "description": "Unauthorized"})
    with pytest.raises(NotifyError, match="HTTP 401: Unauthorized"):
        notifier.send("x")


def test_redacting_formatter_masks_messages_and_tracebacks() -> None:
    register_secrets(["super-secret-value"])
    formatter = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError(f"failed url /bot{TOKEN}/x with super-secret-value")
    except RuntimeError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "token=%s", (TOKEN,), sys.exc_info())
    output = formatter.format(record)
    assert TOKEN not in output and "super-secret-value" not in output
    assert output.count(MASK) >= 3


def test_redact_helpers() -> None:
    assert redact("abc", ["short"]) == "abc"
    assert redact("key=my-private-key-123", ["my-private-key-123"]) == f"key={MASK}"


def test_error_message_is_redacted() -> None:
    text = format_error_message("Books", f"boom /bot{TOKEN}/sendMessage <tag>", WHEN)
    assert TOKEN not in text and "&lt;tag&gt;" in text
