"""Командная строка.

    python -m site_parser run configs/books_toscrape.yaml
    python -m site_parser check configs/books_toscrape.yaml --limit 3
    python -m site_parser history configs/books_toscrape.yaml

Коды выхода (видны в Планировщике Windows и systemd):
0 — успешно, 1 — прогон не удался, 2 — ошибка конфига или окружения,
3 — готово с предупреждениями (часть страниц не загрузилась, не
обновилась Google-таблица, не ушло уведомление), 130 — прервано.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from site_parser import __version__
from site_parser.config import ConfigError, SiteConfig, load_config
from site_parser.fetcher import Fetcher
from site_parser.logging_setup import setup_logging, utf8_stdio
from site_parser.notify import NotifyError, TelegramNotifier, format_error_message
from site_parser.pipeline import EXIT_CONFIG, EXIT_FAILED, PipelineError, RunOptions, run_pipeline
from site_parser.report import fmt_value
from site_parser.scraper import Scraper, build_columns, fill_stats
from site_parser.settings import EnvSettings, load_env
from site_parser.storage import SnapshotStore

log = logging.getLogger("site_parser")


def _add_cache_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--cache",
        dest="use_cache",
        action="store_const",
        const=True,
        help="брать страницы из кэша на диске (для отладки)",
    )
    group.add_argument(
        "--no-cache",
        dest="use_cache",
        action="store_const",
        const=False,
        help="не использовать кэш, даже если он включён в конфиге",
    )
    parser.set_defaults(use_cache=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="site-parser",
        description="Парсер сайтов по YAML-конфигу: CSV, Excel, Google Sheets, отчёт об изменениях, Telegram.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="КОМАНДА")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("config", type=Path, help="путь к YAML-конфигу сайта")
    common.add_argument("--env-file", type=Path, help="файл с секретами (по умолчанию .env в папке проекта)")
    common.add_argument("-v", "--verbose", action="store_true", help="подробный лог")

    run = sub.add_parser("run", parents=[common], help="полный прогон: сбор, выгрузка, сравнение, уведомление")
    run.add_argument("--max-pages", type=int, metavar="N", help="не больше N страниц каталога")
    run.add_argument("--max-items", type=int, metavar="N", help="не больше N записей")
    run.add_argument("--max-images", type=int, metavar="N", help="не больше N картинок (0 — не скачивать)")
    _add_cache_flags(run)
    run.add_argument("--no-notify", action="store_true", help="не отправлять уведомление в Telegram")
    run.add_argument("--no-sheets", action="store_true", help="не выгружать в Google Sheets")
    run.add_argument("--notify-preview", type=Path, metavar="ФАЙЛ", help="сохранить текст уведомления в файл")
    run.add_argument("--no-state", action="store_true", help="не сохранять прогон в историю (пробный запуск)")
    run.add_argument("--note", help="примечание к прогону (попадёт в историю и отчёт)")
    run.add_argument(
        "--log-file",
        type=Path,
        default=Path("logs/site_parser.log"),
        metavar="ФАЙЛ",
        help="файл журнала (по умолчанию logs/site_parser.log)",
    )

    check = sub.add_parser(
        "check", parents=[common], help="проверить селекторы: одна страница, несколько записей, без сохранения"
    )
    check.add_argument("--limit", type=int, default=3, metavar="N", help="сколько записей показать (3)")
    _add_cache_flags(check)

    history = sub.add_parser("history", parents=[common], help="история прогонов из базы")
    history.add_argument("--limit", type=int, default=10, metavar="N")
    return parser


def _notify_failure(config: SiteConfig, env: EnvSettings, error: str) -> None:
    if not (config.telegram.enabled and env.telegram_configured):
        return
    assert env.telegram_token and env.telegram_chat_id
    try:
        TelegramNotifier(env.telegram_token, env.telegram_chat_id).send(
            format_error_message(config.title, error, datetime.now())
        )
    except NotifyError as exc:
        log.error("%s", exc)


def cmd_run(config: SiteConfig, env: EnvSettings, args: argparse.Namespace) -> int:
    options = RunOptions(
        max_pages=args.max_pages,
        max_items=args.max_items,
        max_images=args.max_images,
        use_cache=args.use_cache,
        notify=not args.no_notify,
        notify_preview=args.notify_preview,
        google_sheets=not args.no_sheets,
        save_state=not args.no_state,
        note=args.note,
    )
    try:
        outcome = run_pipeline(config, env, options)
    except PipelineError as exc:
        log.error("Прогон не выполнен: %s", exc)
        if not args.no_notify:
            _notify_failure(config, env, str(exc))
        return EXIT_FAILED
    stats = outcome.scrape.stats
    log.info(
        "Готово за %.0f с: записей %d, страниц %d, ошибок %d, код выхода %d",
        stats.duration_seconds,
        len(outcome.scrape.items),
        stats.pages,
        len(outcome.scrape.errors),
        outcome.exit_code,
    )
    return outcome.exit_code


def cmd_check(config: SiteConfig, env: EnvSettings, args: argparse.Namespace) -> int:
    fetcher = Fetcher.from_settings(config.request, use_cache=args.use_cache)
    try:
        result = Scraper(config, fetcher, max_pages=1, max_items=args.limit).run()
    finally:
        fetcher.close()
    columns = build_columns(config, result.items)
    out = sys.stdout
    for number, item in enumerate(result.items, start=1):
        out.write(f"\n--- Запись {number} ---\n")
        for column in columns:
            if column.name in item:
                out.write(f"  {column.header}: {fmt_value(item.get(column.name), config.output.bool_values)}\n")
    out.write("\nЗаполненность колонок:\n")
    for column, filled in fill_stats(result.items, columns):
        mark = "OK " if filled == len(result.items) else ("ПУСТО" if filled == 0 else "част")
        out.write(f"  [{mark}] {column.header}: {filled} из {len(result.items)}\n")
    for error in result.errors:
        out.write(f"\nОшибка: {error}\n")
    out.flush()
    return 0 if result.items and not result.errors else EXIT_FAILED


def cmd_history(config: SiteConfig, env: EnvSettings, args: argparse.Namespace) -> int:
    runs = SnapshotStore(config.storage.path).list_runs(config.name, args.limit)
    if not runs:
        sys.stdout.write(f"Прогонов «{config.name}» в базе {config.storage.path} ещё нет.\n")
        return 0
    sys.stdout.write(f"{'№':>4}  {'завершён':<16}  {'записей':>7}  {'страниц':>7}  полный  примечание\n")
    for run in runs:
        sys.stdout.write(
            f"{run.id:>4}  {run.finished_at:%d.%m.%Y %H:%M}  {run.items_count:>7}  {run.pages:>7}  "
            f"{'да' if run.complete else 'нет':<6}  {run.note or ''}\n"
        )
    return 0


COMMANDS = {"run": cmd_run, "check": cmd_check, "history": cmd_history}


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    args = build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose, log_file=args.log_file if args.command == "run" else None)
    try:
        load_env(args.env_file)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    env = EnvSettings.from_environ()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("Ошибка конфига: %s", exc)
        return EXIT_CONFIG

    try:
        return COMMANDS[args.command](config, env, args)
    except KeyboardInterrupt:
        log.warning("Остановлено пользователем")
        return 130
    except Exception as exc:
        log.exception("Непредвиденная ошибка: %s", exc)
        if args.command == "run" and not args.no_notify:
            _notify_failure(config, env, f"{type(exc).__name__}: {exc}")
        return EXIT_FAILED
