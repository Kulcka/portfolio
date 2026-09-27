"""Командная строка: `python -m leads_bot <команда>`.

  run            запустить бота (Telegram и/или MAX — по токенам в .env)
  console        демо в терминале, без мессенджера и токенов
  export         выгрузить заявки в Excel
  stats          статистика заявок
  check-config   проверить config.yaml и .env
  seed-demo      заполнить демо-базу вымышленными заявками
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from .app import EXIT_CONFIG_ERROR, TransportsStopped, run_bot
from .config import ConfigError, Settings, load_bot_config, load_settings
from .console import html_to_console
from .logging_setup import setup_logging

log = logging.getLogger("leads_bot")

DEMO_DB = Path("data/demo.sqlite3")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m leads_bot", description="Бот приёма заявок для Telegram и MAX")
    parser.add_argument("--env-file", default=".env", help="файл с переменными окружения (по умолчанию .env)")
    parser.add_argument("--config", help="путь к config.yaml (по умолчанию CONFIG_PATH или ./config.yaml)")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="запустить бота")
    run.add_argument("--only", choices=("telegram", "max"), help="запустить только один мессенджер")

    console = sub.add_parser("console", help="демо в терминале")
    console.add_argument("--db", type=Path, default=DEMO_DB, help=f"база демо-режима (по умолчанию {DEMO_DB})")

    export = sub.add_parser("export", help="выгрузить заявки в Excel")
    export.add_argument("--out", type=Path, help="куда сохранить .xlsx (по умолчанию leads_<дата>.xlsx)")
    export.add_argument("--db", type=Path, help="путь к базе (по умолчанию DATABASE_PATH)")

    stats = sub.add_parser("stats", help="статистика заявок")
    stats.add_argument("--db", type=Path, help="путь к базе (по умолчанию DATABASE_PATH)")

    sub.add_parser("check-config", help="проверить настройки")

    seed = sub.add_parser("seed-demo", help="вымышленные заявки в демо-базу")
    seed.add_argument("--db", type=Path, default=DEMO_DB, help=f"демо-база (по умолчанию {DEMO_DB})")
    seed.add_argument("--count", type=int, default=30)
    return parser


def _run_forever(settings: Settings, only: str | None) -> int:
    config = load_bot_config(settings.config_path)

    async def main() -> None:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        if sys.platform != "win32" and task is not None:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, task.cancel)
        await run_bot(settings, config, only=frozenset({only}) if only else None)

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Остановка по сигналу")
    except TransportsStopped as exc:
        log.critical("Бот остановлен: %s", exc)
        return EXIT_CONFIG_ERROR
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = args.command or "run"
    try:
        settings = load_settings(env_file=args.env_file)
        if args.config:
            settings = dataclasses.replace(settings, config_path=Path(args.config))
        setup_logging(settings.log_level, settings.secrets)

        if command == "run":
            return _run_forever(settings, getattr(args, "only", None))

        config = load_bot_config(settings.config_path)

        if command == "check-config":
            print(f"config.yaml: OK ({settings.config_path}), услуг: {len(config.services)}")
            print(f"Telegram: токен {'задан' if settings.telegram_token else 'не задан'}, "
                  f"админ-чат: {settings.telegram_admin_chat_id or 'не задан'}")
            print(f"MAX: токен {'задан' if settings.max_token else 'не задан'}, режим: {settings.max_mode}, "
                  f"админ-чат: {settings.max_admin_chat_id or 'не задан'}, "
                  f"сертификат: {settings.max_ca_bundle or 'системное хранилище'}")
            print(f"База: {settings.database_path}")
            return 0

        # Импорт здесь, чтобы check-config и run не тянули лишнего.
        from .console import run_console
        from .core.service import LeadService
        from .core.storage import Storage
        from .demo_data import seed_demo_leads

        if command == "console":
            logging.getLogger().setLevel(logging.WARNING)  # чтобы логи не мешали диалогу
            asyncio.run(run_console(config, args.db))
            return 0

        if command == "seed-demo":
            with Storage(args.db) as storage:
                if storage.count_leads():
                    print(f"В {args.db} уже есть заявки — демо-данные не добавлены.", file=sys.stderr)
                    return 1
                seed_demo_leads(storage, config, count=args.count, now=datetime.now(timezone.utc))
            print(f"Добавлено {args.count} вымышленных заявок в {args.db}")
            return 0

        db_path = args.db or settings.database_path
        if not Path(db_path).is_file():
            print(f"База {db_path} не найдена — заявок ещё не было?", file=sys.stderr)
            return 1
        with Storage(db_path) as storage:
            service = LeadService(config, storage)
            if command == "stats":
                print(html_to_console(service.stats_text()))
                return 0
            export = service.export()
            out = args.out or Path(export.filename)
            out.write_bytes(export.content)
            print(f"Выгружено заявок: {export.count} → {out}")
            return 0
    except ConfigError as exc:
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR


if __name__ == "__main__":
    sys.exit(main())
