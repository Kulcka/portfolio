"""Отчёт об изменениях: Markdown-файл и таблица для Excel / Google Sheets."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from site_parser.config import URL_COLUMN, SiteConfig
from site_parser.diff import DiffResult

CHANGE_NEW = "Новый"
CHANGE_PRICE = "Цена"
CHANGE_GONE = "Пропал"
CHANGE_FIELD = "Изменение"

CHANGES_HEADER = ["Тип", "Ключ", "Название", "Поле", "Было", "Стало", "Изменение, %", "Ссылка"]


def fmt_value(value: Any, bool_values: tuple[str, str] = ("да", "нет")) -> str:
    """Значение для человека: «1 299.50», да/нет, списки через запятую, пустое — «—»."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return bool_values[0] if value else bool_values[1]
    if isinstance(value, float):
        return f"{value:,.2f}".replace(",", " ")
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    if isinstance(value, list):
        return ", ".join(fmt_value(v, bool_values) for v in value)
    return str(value)


def fmt_percent(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    return f"{sign}{abs(value):.1f}%"


def _title(item: Mapping[str, Any], title_field: str | None, fallback: str = "") -> str:
    value = item.get(title_field) if title_field else None
    return str(value) if value not in (None, "") else (fallback or str(item.get(URL_COLUMN, "")))


def changes_rows(diff: DiffResult, config: SiteConfig, key_of: Any) -> list[list[Any]]:
    """Строки листа «Изменения» (заголовок — ``CHANGES_HEADER``)."""
    title_field = config.title_field()
    price_field = config.price_field()
    bools = config.output.bool_values
    rows: list[list[Any]] = []
    for item in diff.new:
        rows.append(
            [
                CHANGE_NEW,
                key_of(item),
                _title(item, title_field),
                price_field or "",
                None,
                item.get(price_field) if price_field else None,
                None,
                item.get(URL_COLUMN, ""),
            ]
        )
    for change in diff.price_changes:
        rows.append(
            [
                CHANGE_PRICE,
                change.key,
                change.title,
                price_field or "",
                change.old,
                change.new,
                change.delta_percent,
                change.url,
            ]
        )
    for fc in diff.field_changes:
        rows.append(
            [
                CHANGE_FIELD,
                fc.key,
                fc.title,
                config.label_for(fc.field),
                fmt_value(fc.old, bools),
                fmt_value(fc.new, bools),
                None,
                fc.url,
            ]
        )
    for item in diff.gone:
        rows.append(
            [
                CHANGE_GONE,
                key_of(item),
                _title(item, title_field),
                price_field or "",
                item.get(price_field) if price_field else None,
                None,
                None,
                item.get(URL_COLUMN, ""),
            ]
        )
    return rows


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _md_link(title: str, url: str) -> str:
    title = _md_escape(title)
    return f"[{title}]({url})" if url else title


def render_markdown(
    diff: DiffResult,
    config: SiteConfig,
    *,
    finished_at: datetime,
    pages: int,
    duration_seconds: float,
    errors: list[str],
    note: str | None = None,
) -> str:
    """Отчёт об изменениях в Markdown (читается в любом редакторе и на GitHub)."""
    lines = [f"# Отчёт об изменениях: {config.title}", ""]
    lines.append(f"- Прогон: {finished_at:%d.%m.%Y %H:%M}, страниц: {pages}, время: {duration_seconds:.0f} с")
    lines.append(f"- Записей сейчас: {diff.current_count}")
    if diff.previous_run is not None:
        prev = diff.previous_run
        lines.append(
            f"- Сравнение с прогоном №{prev.id} от {prev.finished_at:%d.%m.%Y %H:%M}: "
            f"было записей {diff.previous_count}"
        )
        if prev.note:
            lines.append(f"- Примечание к прошлому прогону: {prev.note}")
    if note:
        lines.append(f"- Примечание: {note}")
    if errors:
        lines.append(f"- Ошибок загрузки: {len(errors)} (прогон неполный)")
    lines.append("")

    if diff.is_first_run:
        lines += ["Первый прогон — сравнивать не с чем. Следующий прогон покажет изменения.", ""]
    else:
        lines += _diff_sections(diff, config)

    if errors:
        lines += ["## Ошибки загрузки", ""]
        lines += [f"- {_md_escape(e)}" for e in errors[:50]]
        if len(errors) > 50:
            lines.append(f"- …и ещё {len(errors) - 50}")
        lines.append("")
    return "\n".join(lines)


def _diff_sections(diff: DiffResult, config: SiteConfig) -> list[str]:
    title_field = config.title_field()
    price_field = config.price_field()
    bools = config.output.bool_values
    counts = diff.counts()
    lines: list[str] = []
    lines += [
        "| Изменение | Записей |",
        "|---|---:|",
        f"| Новые | {counts['new']} |",
        f"| Изменилась цена | {counts['price']} |",
        f"| Пропали | {counts['gone']} |",
    ]
    if config.changes.track:
        lines.append(
            f"| Другие изменения ({', '.join(config.label_for(t) for t in config.changes.track)}) | {counts['other']} |"
        )
    lines.append("")
    if not diff.has_changes:
        lines += ["Изменений нет.", ""]

    if diff.new:
        lines += [f"## Новые ({len(diff.new)})", ""]
        lines += ["| Название | Цена |", "|---|---:|"]
        for item in diff.new:
            price = fmt_value(item.get(price_field)) if price_field else "—"
            lines.append(f"| {_md_link(_title(item, title_field), str(item.get(URL_COLUMN, '')))} | {price} |")
        lines.append("")

    if diff.price_changes:
        lines += [f"## Изменилась цена ({len(diff.price_changes)})", ""]
        lines += ["| Название | Было | Стало | Изменение |", "|---|---:|---:|---:|"]
        for change in diff.price_changes:
            lines.append(
                f"| {_md_link(change.title, change.url)} | {fmt_value(change.old)} | "
                f"{fmt_value(change.new)} | {fmt_percent(change.delta_percent)} |"
            )
        lines.append("")

    if diff.field_changes:
        lines += [f"## Другие изменения ({len(diff.field_changes)})", ""]
        lines += ["| Название | Поле | Было | Стало |", "|---|---|---|---|"]
        for fc in diff.field_changes:
            lines.append(
                f"| {_md_link(fc.title, fc.url)} | {config.label_for(fc.field)} | "
                f"{_md_escape(fmt_value(fc.old, bools))} | {_md_escape(fmt_value(fc.new, bools))} |"
            )
        lines.append("")

    if diff.gone:
        lines += [f"## Пропали ({len(diff.gone)})", ""]
        lines += ["| Название | Последняя цена |", "|---|---:|"]
        for item in diff.gone:
            price = fmt_value(item.get(price_field)) if price_field else "—"
            lines.append(f"| {_md_link(_title(item, title_field), str(item.get(URL_COLUMN, '')))} | {price} |")
        lines.append("")
    if diff.gone_suppressed_reason:
        lines += [f"Раздел «Пропали» не заполнен: {diff.gone_suppressed_reason}.", ""]
    return lines
