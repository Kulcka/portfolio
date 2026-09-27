"""Единый CLI: doctools <команда> ...

Каждая команда печатает краткий итог и сохраняет подробный отчёт в Markdown
рядом с результатом (<имя>_отчёт.md или отчёт_<команда>.md в папке результата).
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from . import DocToolsError, __version__
from .report import Report

EXIT_OK, EXIT_USER, EXIT_BUG = 0, 2, 3


def _split(s: str | None) -> list[str] | None:
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def _report_path(output: Path, command: str) -> Path:
    if output.suffix:
        return output.with_name(f"{output.stem}_отчёт.md")
    return output / f"отчёт_{command}.md"


# ------------------------------------------------------------ команды
def cmd_pdf2xlsx(a: argparse.Namespace) -> tuple[Path, Report]:
    from .pdf_tables import pdf_to_xlsx

    return pdf_to_xlsx(Path(a.input), Path(a.output) if a.output else None, strategy=a.strategy,
                       pages=a.pages, source_page=a.source_page)


def cmd_pdf2docx(a: argparse.Namespace) -> tuple[Path, Report]:
    from .pdf_text import pdf_to_docx

    return pdf_to_docx(Path(a.input), Path(a.output) if a.output else None, pages=a.pages, tables=not a.no_tables)


def cmd_pdftext(a: argparse.Namespace) -> tuple[Path, Report]:
    from .pdf_text import pdf_to_text

    return pdf_to_text(Path(a.input), Path(a.output) if a.output else None, pages=a.pages, tables=not a.no_tables)


def cmd_merge(a: argparse.Namespace) -> tuple[Path, Report]:
    from .merge import merge_files

    return merge_files(a.inputs, Path(a.output), Path(a.config) if a.config else None, a.sheet)


def cmd_clean(a: argparse.Namespace) -> tuple[Path, Report]:
    from .clean import clean_file

    return clean_file(Path(a.input), Path(a.output) if a.output else None, sheet=a.sheet, header_row=a.header_row,
                      type_overrides=a.type, dedup_keys=_split(a.dedup_keys), fuzzy_keys=_split(a.fuzzy_keys),
                      fuzzy_threshold=a.fuzzy_threshold, drop_fuzzy=a.drop_fuzzy, keep_empty=a.keep_empty)


def cmd_docx_fill(a: argparse.Namespace) -> tuple[Path, Report]:
    from .docx_fill import fill_documents

    out = Path(a.output)
    _, rep = fill_documents(Path(a.template), Path(a.data), out, name_pattern=a.name, sheet=a.sheet,
                            allow_missing=a.allow_missing, to_pdf=a.pdf)
    return out, rep


def cmd_convert(a: argparse.Namespace) -> tuple[Path, Report]:
    from .convert import convert_files

    out = Path(a.output)
    sep = "\t" if a.sep in ("tab", "\\t") else a.sep
    _, rep = convert_files(a.inputs, a.to, out, sep=sep, encoding=a.encoding, keep_text=a.keep_text, sheet=a.sheet)
    return out, rep


# ------------------------------------------------------------ парсер
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="doctools",
        description="Обработка документов и таблиц: PDF -> Excel/Word, чистка, объединение, шаблоны, конвертация.",
        epilog="Подробная справка по команде: doctools <команда> -h")
    p.add_argument("--version", action="version", version=f"doctools {__version__}")
    p.add_argument("-q", "--quiet", action="store_true", help="не печатать итог (отчёт всё равно сохраняется)")
    p.add_argument("--debug", action="store_true", help="при ошибке показать полную трассировку")
    sub = p.add_subparsers(dest="command", metavar="<команда>", required=True)

    s = sub.add_parser("pdf2xlsx", help="таблицы из PDF -> Excel")
    s.add_argument("input", help="PDF с текстовым слоем")
    s.add_argument("-o", "--output", help="куда сохранить .xlsx (по умолчанию рядом с PDF)")
    s.add_argument("--strategy", default="auto", choices=["auto", "lines", "rows", "text"],
                   help="как искать таблицы: auto — сетка, затем линии строк; lines — только сетка; "
                        "rows — линии строк + колонки по просветам; text — только выравнивание текста")
    s.add_argument("--pages", help="страницы, например 1-3,5")
    s.add_argument("--source-page", action="store_true", help="добавить колонку с номером страницы PDF")
    s.set_defaults(func=cmd_pdf2xlsx)

    for name, func, ext in (("pdf2docx", cmd_pdf2docx, ".docx"), ("pdftext", cmd_pdftext, ".txt")):
        s = sub.add_parser(name, help=f"текст PDF -> {ext[1:].upper()} с абзацами и заголовками")
        s.add_argument("input", help="PDF с текстовым слоем")
        s.add_argument("-o", "--output", help=f"куда сохранить {ext} (по умолчанию рядом с PDF)")
        s.add_argument("--pages", help="страницы, например 1-3,5")
        s.add_argument("--no-tables", action="store_true", help="таблицы не выделять, выводить как текст")
        s.set_defaults(func=func)

    s = sub.add_parser("merge", help="объединить много Excel/CSV/JSON в одну таблицу")
    s.add_argument("inputs", nargs="+", help="файлы, папки или маски (*.xlsx)")
    s.add_argument("-o", "--output", required=True, help="итоговый .xlsx")
    s.add_argument("--config", help="словарь колонок и синонимов (YAML); по умолчанию встроенный columns.yaml")
    s.add_argument("--sheet", help="брать только этот лист (по умолчанию все непустые)")
    s.set_defaults(func=cmd_merge)

    s = sub.add_parser("clean", help="чистка таблицы: дубли, пробелы, телефоны, ИНН, даты, числа")
    s.add_argument("input", help=".xlsx или .csv")
    s.add_argument("-o", "--output", help="результат .xlsx (с листами до/после и журналом) или .csv")
    s.add_argument("--sheet", help="лист Excel (по умолчанию первый непустой)")
    s.add_argument("--header-row", type=int, help="номер строки заголовка (по умолчанию определяется сам)")
    s.add_argument("--type", action="append", metavar="КОЛОНКА=ТИП",
                   help="тип колонки вручную: телефон, ИНН, e-mail, дата, число, имя, текст, пропустить "
                        "(можно несколько раз)")
    s.add_argument("--dedup-keys", help="колонки-ключ точных дублей через запятую (по умолчанию все колонки)")
    s.add_argument("--fuzzy-keys", help="колонки для поиска похожих строк (опечатки), через запятую")
    s.add_argument("--fuzzy-threshold", type=int, default=90, help="порог похожести 0–100 (по умолчанию 90)")
    s.add_argument("--drop-fuzzy", action="store_true", help="удалять похожие строки, а не только помечать")
    s.add_argument("--keep-empty", action="store_true", help="не удалять пустые строки")
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser("docx-fill", help="заполнить шаблон Word по строкам таблицы ({{поле}})")
    s.add_argument("template", help="шаблон .docx с плейсхолдерами {{Колонка}}")
    s.add_argument("data", help="таблица .xlsx/.csv: одна строка = один документ")
    s.add_argument("-o", "--output", required=True, help="папка для готовых документов")
    s.add_argument("--name", help="шаблон имени файла, например \"Договор_{{Номер}}_{{Компания}}\" "
                                  "({{№}} — номер строки)")
    s.add_argument("--sheet", help="лист Excel с данными")
    s.add_argument("--allow-missing", action="store_true", help="разрешить плейсхолдеры без колонки (будут пустыми)")
    s.add_argument("--pdf", action="store_true", help="сделать также PDF через Microsoft Word")
    s.set_defaults(func=cmd_docx_fill)

    s = sub.add_parser("convert", help="пакетная конвертация XLSX/CSV/JSON и DOCX -> PDF")
    s.add_argument("inputs", nargs="+", help="файлы, папки или маски")
    s.add_argument("--to", required=True, help="xlsx, csv, json, pdf или docx")
    s.add_argument("-o", "--output", required=True, help="папка для результатов")
    s.add_argument("--sep", default=";", help="разделитель CSV (по умолчанию «;», для табуляции — tab)")
    s.add_argument("--encoding", default="utf-8-sig", help="кодировка CSV (utf-8-sig — для Excel, cp1251 — для 1С)")
    s.add_argument("--keep-text", action="store_true", help="CSV/JSON -> XLSX: не превращать строки в числа и даты")
    s.add_argument("--sheet", help="из Excel брать только этот лист (по умолчанию все)")
    s.set_defaults(func=cmd_convert)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        output, rep = args.func(args)
        rpath = rep.save(_report_path(Path(output), args.command))
        if not args.quiet:
            print(rep.console_summary())
            print(f"  Отчёт: {rpath}")
        return EXIT_OK
    except DocToolsError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return EXIT_USER
    except KeyboardInterrupt:
        print("Прервано.", file=sys.stderr)
        return EXIT_USER
    except Exception as exc:  # непредвиденная ошибка — коротко, трассировка по --debug
        if args.debug:
            traceback.print_exc()
        else:
            print(f"Внутренняя ошибка: {type(exc).__name__}: {exc}\nЗапустите с --debug и пришлите вывод.", file=sys.stderr)
        return EXIT_BUG


if __name__ == "__main__":
    sys.exit(main())
