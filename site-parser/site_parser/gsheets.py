"""Выгрузка в Google Sheets через сервисный аккаунт (gspread).

Нужно: JSON-ключ сервисного аккаунта (путь — ``GOOGLE_SERVICE_ACCOUNT_FILE``
в ``.env``) и таблица, к которой у почты сервисного аккаунта есть доступ
«Редактор». Пошагово — в README.

Лист перезаписывается целиком: очистка → нужный размер → данные одним
запросом. Значения пишутся как ``RAW``, чтобы строки со страницы сайта не
исполнялись в таблице как формулы.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from site_parser.config import GoogleSheetsSpec
from site_parser.exporters import Table

log = logging.getLogger(__name__)

ClientFactory = Callable[[Path], Any]


class SheetsError(Exception):
    """Не удалось выгрузить в Google Sheets. Текст пригоден для показа пользователю."""


def _service_account_email(credentials_file: Path) -> str | None:
    try:
        data = json.loads(credentials_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    email = data.get("client_email") if isinstance(data, dict) else None
    return str(email) if email else None


def _default_client(credentials_file: Path) -> Any:
    import gspread

    return gspread.service_account(filename=str(credentials_file))


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def _write_worksheet(spreadsheet: Any, title: str, values: list[list[Any]]) -> None:
    import gspread

    rows = max(len(values), 2)
    cols = max((len(r) for r in values), default=1)
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)
    worksheet.clear()
    worksheet.resize(rows=rows, cols=cols)
    worksheet.update(values=values, range_name="A1", value_input_option="RAW")
    try:  # оформление — не критично, данные уже записаны
        worksheet.freeze(rows=1)
        worksheet.format("1:1", {"textFormat": {"bold": True}})
        worksheet.set_basic_filter()
    except Exception as exc:
        log.debug("Google Sheets: оформление листа %s не применено: %s", title, exc)


def export_google_sheets(
    spec: GoogleSheetsSpec,
    credentials_file: Path | None,
    header: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    changes: Table | None = None,
    client_factory: ClientFactory | None = None,
) -> str:
    """Записать данные (и лист изменений) в таблицу. Вернуть адрес таблицы."""
    if credentials_file is None:
        raise SheetsError("не задан ключ сервисного аккаунта: GOOGLE_SERVICE_ACCOUNT_FILE в .env")
    if not credentials_file.is_file():
        raise SheetsError(f"файл ключа сервисного аккаунта не найден: {credentials_file}")
    try:
        import gspread
        from google.auth.exceptions import GoogleAuthError
    except ImportError:
        raise SheetsError("не установлен пакет gspread: pip install -r requirements.txt") from None

    factory = client_factory or _default_client
    target = spec.spreadsheet.strip()
    try:
        client = factory(credentials_file)
        by_url = target.startswith("https://")
        spreadsheet = client.open_by_url(target) if by_url else client.open_by_key(target)
        _write_worksheet(spreadsheet, spec.worksheet, [list(header)] + [[_cell(v) for v in r] for r in rows])
        if changes is not None and spec.changes_worksheet:
            change_rows = [[_cell(v) for v in r] for r in changes.rows] or [["Изменений нет"]]
            _write_worksheet(spreadsheet, spec.changes_worksheet, [list(changes.header), *change_rows])
    except gspread.SpreadsheetNotFound:
        email = _service_account_email(credentials_file) or "почта сервисного аккаунта"
        raise SheetsError(f"таблица не найдена или нет доступа — откройте её для {email} с правом «Редактор»") from None
    except gspread.exceptions.APIError as exc:
        raise SheetsError(f"ошибка Google API: {exc}") from None
    except (GoogleAuthError, ValueError, OSError) as exc:  # битый ключ, нет сети
        raise SheetsError(f"не удалось подключиться к Google Sheets: {type(exc).__name__}: {exc}") from None
    return str(getattr(spreadsheet, "url", target))
