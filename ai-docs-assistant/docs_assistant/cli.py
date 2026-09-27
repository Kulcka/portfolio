"""Командная строка.

Примеры::

    python -m docs_assistant ingest demo_docs
    python -m docs_assistant ask "Сколько стоит доставка посылки до 5 кг?"
    python -m docs_assistant search "возврат товара" --k 5
    python -m docs_assistant eval
    python -m docs_assistant bot
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from docs_assistant.citations import source_label
from docs_assistant.config import PROVIDERS, ConfigError, Settings
from docs_assistant.index import IndexBuildError
from docs_assistant.llm.base import LLMError
from docs_assistant.security import redact, setup_logging

logger = logging.getLogger("docs_assistant")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docs_assistant", description="ИИ-консультант по документам компании"
    )
    parser.add_argument("--env", type=Path, help="путь к файлу .env (по умолчанию ./.env)")
    parser.add_argument("--provider", choices=PROVIDERS, help="провайдер модели (перекрывает LLM_PROVIDER)")
    parser.add_argument("--docs", type=Path, help="папка с документами (перекрывает DOCS_DIR)")
    parser.add_argument("--index", type=Path, help="папка индекса (перекрывает INDEX_DIR)")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный журнал")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="проиндексировать папку с документами")
    ingest.add_argument("folder", nargs="?", type=Path, help="папка (по умолчанию DOCS_DIR)")

    ask = sub.add_parser("ask", help="задать вопрос")
    ask.add_argument("question", nargs="+", help="текст вопроса")
    ask.add_argument("--json", action="store_true", help="вывести ответ в JSON")
    ask.add_argument("--debug", action="store_true", help="показать найденные фрагменты")

    search = sub.add_parser("search", help="только поиск, без модели")
    search.add_argument("query", nargs="+")
    search.add_argument("--k", type=int, default=5)

    evaluate = sub.add_parser("eval", help="оценка качества поиска и отказов")
    evaluate.add_argument("--questions", type=Path, default=Path("eval/questions.json"))
    evaluate.add_argument("--tune", action="store_true", help="подобрать порог MIN_COVERAGE на dev-части")
    evaluate.add_argument("--k", type=int, default=3)

    sub.add_parser("bot", help="запустить Telegram-бота")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env(dotenv_path=args.env)
        overrides: dict[str, object] = {}
        if args.provider:
            overrides["llm_provider"] = args.provider
        if args.docs:
            overrides["docs_dir"] = args.docs.resolve()
        if getattr(args, "folder", None):
            overrides["docs_dir"] = args.folder.resolve()
        if args.index:
            overrides["index_dir"] = args.index.resolve()
        if overrides:
            settings = dataclasses.replace(settings, **overrides)  # type: ignore[arg-type]
        setup_logging("DEBUG" if args.verbose else settings.log_level)
        return _dispatch(args, settings)
    except (ConfigError, IndexBuildError, LLMError) as exc:
        print(f"Ошибка: {redact(str(exc))}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _dispatch(args: argparse.Namespace, settings: Settings) -> int:
    from docs_assistant.assistant import DocsAssistant

    if args.command == "bot":
        from docs_assistant.telegram_bot import run_bot

        run_bot(settings)
        return 0

    if args.command == "eval":
        from docs_assistant.evaluation import run_eval_cli

        return run_eval_cli(settings, args.questions, k=args.k, tune=args.tune)

    assistant = DocsAssistant.from_settings(settings)
    try:
        if args.command == "ingest":
            report = assistant.reindex()
            print(report.summary())
            return 1 if report.failed and not report.total_files else 0

        assistant.reindex()  # дёшево: неизменённые файлы не перечитываются
        if args.command == "search":
            result = assistant.search(" ".join(args.query), k=args.k)
            print(f"Решение: {'есть ответ' if result.relevant else 'нет ответа'} ({result.reason})")
            for number, hit in enumerate(result.hits, start=1):
                label = source_label(hit.chunk.doc, hit.chunk.page, hit.chunk.section)
                preview = " ".join(hit.chunk.text.split())[:160]
                print(f"{number}. {label}  bm25={hit.bm25:.2f} совпадение={hit.coverage:.2f}\n   {preview}")
            return 0

        answer = assistant.ask(" ".join(args.question))
        if args.json:
            print(
                json.dumps(
                    {
                        "status": answer.status,
                        "answer": answer.text,
                        "sources": [dataclasses.asdict(s) for s in answer.sources],
                        "reason": answer.reason,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(answer.render())
            if args.debug:
                print(f"\n[статус: {answer.status}; {answer.reason}]")
                for hit in answer.hits:
                    print(f"  {source_label(hit.chunk.doc, hit.chunk.page, hit.chunk.section)} "
                          f"совпадение={hit.coverage:.2f}")
        return 0 if answer.status != "error" else 1
    finally:
        assistant.close()


def _utf8_console() -> None:
    """В консоли Windows (cp866/cp1251) кириллица и «•» иначе печатаются с ошибкой."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
