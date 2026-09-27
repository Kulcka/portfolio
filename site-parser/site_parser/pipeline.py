"""Прогон целиком: сбор → картинки → сравнение с прошлым → выгрузка → уведомление.

Порядок выбран так, чтобы сбой на любом шаге не портил уже готовое:

1. если не собрано ни одной записи — ничего не перезаписываем и не
   сохраняем (иначе сломанный сайт «обнулит» таблицу заказчика);
2. прошлый прогон читается до записи текущего;
3. сбой Google Sheets или Telegram не отменяет локальные файлы — прогон
   завершается с кодом 3 («готово с предупреждениями»).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from site_parser.config import SiteConfig
from site_parser.diff import DiffResult, compute_diff, item_key
from site_parser.exporters import ExportError, ExportOptions, Table, export_csv, export_json, export_xlsx, rows_for
from site_parser.fetcher import Fetcher
from site_parser.gsheets import ClientFactory, SheetsError, export_google_sheets
from site_parser.images import download_images
from site_parser.notify import NotifyError, TelegramNotifier, format_changes_message
from site_parser.report import CHANGES_HEADER, changes_rows, render_markdown
from site_parser.scraper import Scraper, ScrapeResult, build_columns, fill_stats
from site_parser.settings import EnvSettings
from site_parser.storage import RunInfo, SnapshotStore

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_WARNINGS = 3


class PipelineError(Exception):
    """Прогон не удался целиком (например, не собрано ни одной записи)."""


@dataclass
class RunOptions:
    max_pages: int | None = None
    max_items: int | None = None
    max_images: int | None = None  # None — как в конфиге, 0 — не скачивать
    use_cache: bool | None = None  # None — как в конфиге
    notify: bool = True
    notify_preview: Path | None = None
    google_sheets: bool = True
    save_state: bool = True
    note: str | None = None


@dataclass
class RunOutcome:
    scrape: ScrapeResult
    diff: DiffResult
    run: RunInfo | None
    files: list[Path] = field(default_factory=list)
    sheet_url: str | None = None
    notification: str | None = None
    notified: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.scrape.complete and not self.warnings else EXIT_WARNINGS


def _summary_rows(config: SiteConfig, result: ScrapeResult, diff: DiffResult) -> list[tuple[str, Any]]:
    stats = result.stats
    finished = stats.finished_at or datetime.now().astimezone()
    start = config.start_urls[0] + (f" (+ещё {len(config.start_urls) - 1})" if len(config.start_urls) > 1 else "")
    rows: list[tuple[str, Any]] = [
        ("Источник", config.title),
        ("Стартовый адрес", start),
        ("Прогон завершён", f"{finished:%d.%m.%Y %H:%M}"),
        ("Длительность, с", round(stats.duration_seconds)),
        ("Страниц каталога", stats.list_pages),
        ("Карточек товаров", stats.detail_pages),
        ("Записей", len(result.items)),
        ("Картинок скачано", stats.images),
        ("Ошибок загрузки", len(result.errors)),
    ]
    if diff.previous_run is None:
        rows.append(("Сравнение", "первый прогон"))
    else:
        c = diff.counts()
        rows.append(
            ("Сравнение с прогоном", f"№{diff.previous_run.id} от {diff.previous_run.finished_at:%d.%m.%Y %H:%M}")
        )
        rows.append(("Новые / цена / пропали", f"{c['new']} / {c['price']} / {c['gone']}"))
    return rows


def _log_fill(result: ScrapeResult, config: SiteConfig) -> None:
    total = len(result.items)
    for column, filled in fill_stats(result.items, build_columns(config, result.items)):
        if column.name == (config.images.column if config.images else None):
            continue
        if filled == 0:
            log.warning("Колонка «%s» пустая у всех %d записей — проверьте селектор", column.header, total)
        elif filled < total:
            log.info("Колонка «%s» заполнена у %d из %d записей", column.header, filled, total)


def _write_report(output_dir: Path, text: str, finished: datetime, keep: int) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    latest = output_dir / "changes.md"
    latest.write_text(text, encoding="utf-8")
    archive_dir = output_dir / "reports"
    archive_dir.mkdir(exist_ok=True)
    (archive_dir / f"changes_{finished:%Y-%m-%d_%H%M%S}.md").write_text(text, encoding="utf-8")
    for old in sorted(archive_dir.glob("changes_*.md"))[:-keep]:
        old.unlink(missing_ok=True)
    return latest


def run_pipeline(
    config: SiteConfig,
    env: EnvSettings,
    options: RunOptions | None = None,
    *,
    fetcher: Fetcher | None = None,
    sheets_client_factory: ClientFactory | None = None,
    notifier_factory: Callable[[str, str], TelegramNotifier] | None = None,
) -> RunOutcome:
    options = options or RunOptions()
    own_fetcher = fetcher is None
    active_fetcher = fetcher or Fetcher.from_settings(config.request, use_cache=options.use_cache)
    output_dir = Path(config.output.dir)

    def key_of(item: dict[str, Any]) -> str:
        return item_key(item, config.changes.key)

    # 1. Сбор
    try:
        log.info("Прогон «%s»: %s", config.title, ", ".join(config.start_urls[:3]))
        result = Scraper(config, active_fetcher, max_pages=options.max_pages, max_items=options.max_items).run()
        if not result.items:
            detail = f" Ошибки: {'; '.join(result.errors[:3])}" if result.errors else ""
            raise PipelineError("не собрано ни одной записи — файлы и история не изменены." + detail)
        if config.images:
            limit = options.max_images if options.max_images is not None else config.images.max
            if limit != 0:
                img = download_images(result.items, config.images, active_fetcher, output_dir, key_of, limit=limit)
                result.stats.images = img.downloaded
                result.stats.images_failed = img.failed
                log.info("Картинки: скачано %d, уже были %d, ошибок %d", img.downloaded, img.existing, img.failed)
                result.stats.finished_at = datetime.now().astimezone()  # время прогона — с картинками
    finally:
        if own_fetcher:
            active_fetcher.close()
    fstats = active_fetcher.stats
    log.info(
        "Сбор завершён: страниц %d (запросов %d, из кэша %d, повторов %d), записей %d, повторов записей %d",
        result.stats.pages,
        fstats.requests,
        fstats.cache_hits,
        fstats.retries,
        len(result.items),
        result.stats.duplicates,
    )
    _log_fill(result, config)

    # 2. Сравнение с прошлым прогоном и сохранение текущего
    current = {key_of(item): item for item in result.items}
    store = SnapshotStore(config.storage.path)
    previous_run = store.latest_run(config.name)
    previous_items = store.load_items(previous_run.id) if previous_run else None
    diff = compute_diff(
        previous_items,
        current,
        config.changes,
        title_field=config.title_field(),
        price_field=config.price_field(),
        current_complete=result.complete,
        previous_run=previous_run,
    )
    run_info: RunInfo | None = None
    finished = result.stats.finished_at or datetime.now().astimezone()
    if options.save_state:
        run_info = store.save_run(
            config.name,
            current,
            started_at=result.stats.started_at,
            finished_at=finished,
            complete=result.complete,
            pages=result.stats.pages,
            note=options.note,
        )
        store.prune(config.name, config.storage.keep_runs)
    outcome = RunOutcome(scrape=result, diff=diff, run=run_info)

    # 3. Выгрузка
    columns = build_columns(config, result.items)
    opts = ExportOptions(
        delimiter=config.output.csv_delimiter,
        list_separator=config.output.list_separator,
        bool_values=config.output.bool_values,
    )
    changes_table = None if diff.is_first_run else Table(CHANGES_HEADER, changes_rows(diff, config, key_of))
    base = output_dir / config.output.basename
    for fmt in config.output.formats:
        try:
            if fmt == "csv":
                outcome.files.append(export_csv(base.with_suffix(".csv"), columns, result.items, opts))
            elif fmt == "xlsx":
                outcome.files.append(
                    export_xlsx(
                        base.with_suffix(".xlsx"),
                        columns,
                        result.items,
                        opts,
                        sheet_name=config.output.sheet_name,
                        changes=changes_table,
                        summary=_summary_rows(config, result, diff),
                    )
                )
            elif fmt == "json":
                outcome.files.append(export_json(base.with_suffix(".json"), columns, result.items))
        except ExportError as exc:
            log.error("Выгрузка %s: %s", fmt.upper(), exc)
            outcome.warnings.append(f"{fmt}: {exc}")

    report = render_markdown(
        diff,
        config,
        finished_at=finished,
        pages=result.stats.pages,
        duration_seconds=result.stats.duration_seconds,
        errors=result.errors,
        note=options.note,
    )
    outcome.files.append(_write_report(output_dir, report, finished, config.storage.keep_runs))

    sheets = config.output.google_sheets
    if sheets.enabled and options.google_sheets:
        try:
            outcome.sheet_url = export_google_sheets(
                sheets,
                env.google_credentials_file,
                [c.header for c in columns],
                rows_for(result.items, columns, opts),
                changes=changes_table,
                client_factory=sheets_client_factory,
            )
            log.info("Google Sheets обновлена: %s", outcome.sheet_url)
        except SheetsError as exc:
            log.error("Google Sheets: %s", exc)
            outcome.warnings.append(f"Google Sheets: {exc}")
    elif not sheets.enabled:
        log.info("Google Sheets не настроена (output.google_sheets.spreadsheet) — пропуск")

    # 4. Уведомление
    outcome.notification = format_changes_message(
        diff,
        config,
        finished_at=finished,
        errors_count=len(result.errors),
        max_lines=config.telegram.max_lines,
    )
    if options.notify_preview is not None:
        options.notify_preview.parent.mkdir(parents=True, exist_ok=True)
        options.notify_preview.write_text(outcome.notification, encoding="utf-8")
    _maybe_notify(config, env, options, outcome, notifier_factory)

    c = diff.counts()
    if diff.is_first_run:
        log.info("Первый прогон — сравнивать не с чем")
    else:
        log.info(
            "Изменения: новых %d, цена изменилась %d, пропало %d, прочих %d",
            c["new"],
            c["price"],
            c["gone"],
            c["other"],
        )
    log.info("Файлы: %s", ", ".join(str(p) for p in outcome.files))
    return outcome


def _maybe_notify(
    config: SiteConfig,
    env: EnvSettings,
    options: RunOptions,
    outcome: RunOutcome,
    notifier_factory: Callable[[str, str], TelegramNotifier] | None,
) -> None:
    if not (options.notify and config.telegram.enabled):
        return
    if not env.telegram_configured:
        log.info("Telegram не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID в .env) — уведомление пропущено")
        return
    important = outcome.diff.has_changes or bool(outcome.scrape.errors)
    if config.telegram.only_changes and not important:
        log.info("Изменений нет — уведомление не отправляется (notify.telegram.only_changes)")
        return
    assert env.telegram_token and env.telegram_chat_id and outcome.notification
    factory = notifier_factory or TelegramNotifier
    try:
        factory(env.telegram_token, env.telegram_chat_id).send(outcome.notification)
        outcome.notified = True
    except NotifyError as exc:
        log.error("%s", exc)
        outcome.warnings.append(str(exc))
