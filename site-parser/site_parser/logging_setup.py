"""Настройка логов: консоль + файл с ротацией, секреты маскируются везде."""

from __future__ import annotations

import contextlib
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from site_parser.redact import RedactingFormatter

CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def utf8_stdio() -> None:
    """Вывод в UTF-8: иначе при перенаправлении в файл (Планировщик Windows)
    кириллица пишется в кодировке консоли или падает с ошибкой."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def setup_logging(*, verbose: bool = False, log_file: Path | None = None) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(RedactingFormatter(CONSOLE_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(RedactingFormatter(FILE_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        root.addHandler(file_handler)

    # Сторонние библиотеки пишут в DEBUG адреса запросов — не засоряем лог.
    for noisy in ("urllib3", "charset_normalizer", "google", "gspread", "chardet"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
