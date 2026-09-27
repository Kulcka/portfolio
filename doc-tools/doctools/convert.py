"""convert: пакетная конвертация.

Таблицы: XLSX <-> CSV <-> JSON (каждый лист Excel — отдельный CSV; в JSON — объект
по листам). CSV по умолчанию — «;» и UTF-8 с BOM: так его правильно открывает
русский Excel. При переводе CSV/JSON -> XLSX числа и даты становятся числами и
датами Excel (коды, ИНН, телефоны и строки с ведущими нулями остаются текстом).

Документы: DOCX/DOC/RTF/ODT -> PDF или DOCX через установленный Microsoft Word.
"""

from __future__ import annotations

from pathlib import Path

from . import DocToolsError
from .merge import expand_inputs
from .report import Report
from .tableio import TABLE_EXT, Sheet, infer_and_convert, read_tables, write_csv, write_json, write_xlsx

DOC_EXT = {".docx", ".doc", ".rtf", ".odt", ".docm"}
TABLE_TARGETS = {"xlsx", "csv", "json"}
DOC_TARGETS = {"pdf", "docx"}


def convert_files(inputs: list[str | Path], to: str, out_dir: Path, *, sep: str = ";",
                  encoding: str = "utf-8-sig", keep_text: bool = False,
                  sheet: str | None = None) -> tuple[list[Path], Report]:
    to = to.lower().lstrip(".")
    if to not in TABLE_TARGETS | DOC_TARGETS:
        raise DocToolsError(f"Неизвестный формат «{to}». Варианты: {', '.join(sorted(TABLE_TARGETS | DOC_TARGETS))}")
    files = expand_inputs(inputs, TABLE_EXT | DOC_EXT)
    out_dir = Path(out_dir)
    rep = Report(f"Конвертация в {to.upper()}: {len(files)} файл(ов)")
    made: list[Path] = []
    skipped: list[list[str]] = []

    doc_jobs = []
    for f in files:
        ext = f.suffix.lower()
        if ext in DOC_EXT:
            if to not in DOC_TARGETS:
                skipped.append([f.name, f"документ Word нельзя превратить в {to}"])
            elif ext == ".docx" and to == "docx":
                skipped.append([f.name, "уже DOCX"])
            else:
                doc_jobs.append(f)
            continue
        if ext not in TABLE_EXT or to not in TABLE_TARGETS:
            skipped.append([f.name, f"{ext} -> {to} не поддерживается"])
            continue
        if ext.lstrip(".") == to:
            skipped.append([f.name, f"уже {to.upper()}"])
            continue
        made += _convert_table(f, to, out_dir, sep, encoding, keep_text, rep, sheet)

    if doc_jobs:
        from .word_com import WordSession

        with WordSession() as word:
            done = 0
            for f in doc_jobs:
                dst = out_dir / f"{f.stem}.{to}"
                try:
                    made.append(word.to_pdf(f, dst) if to == "pdf" else word.to_docx(f, dst))
                    done += 1
                except DocToolsError as exc:  # один битый файл не останавливает пакет
                    rep.error(str(exc))
        rep.info(f"Документы сконвертированы через Microsoft Word: {done} из {len(doc_jobs)}")
        if word.killed:
            rep.warn(f"Word не закрылся сам — завершён принудительно (PID {', '.join(map(str, word.killed))})")

    rep.stat("Файлов на входе", len(files))
    rep.stat("Файлов создано", len(made))
    rep.stat("Пропущено", len(skipped))
    for s in skipped:
        rep.warn(f"{s[0]}: пропущен — {s[1]}")
    for p in made:
        rep.output(p)
    return made, rep


def _convert_table(f: Path, to: str, out_dir: Path, sep: str, encoding: str, keep_text: bool, rep: Report,
                   sheet: str | None = None) -> list[Path]:
    tables = read_tables(f, sheet if f.suffix.lower() in (".xlsx", ".xlsm") else None)
    if not tables:
        rep.warn(f"{f.name}: нет данных — пропущен")
        return []
    if to == "json":
        return [write_json(out_dir / f"{f.stem}.json", tables)]
    if to == "csv":
        out = []
        for t in tables:
            suffix = "" if len(tables) == 1 else "_" + t.name.split(" / ")[-1]
            out.append(write_csv(out_dir / f"{f.stem}{suffix}.csv", t, sep=sep, encoding=encoding))
        if len(tables) > 1:
            rep.info(f"{f.name}: {len(tables)} листа — по CSV на лист")
        return out
    # -> xlsx
    sheets = []
    for t in tables:
        if not keep_text and f.suffix.lower() in (".csv", ".json"):
            typing = infer_and_convert(t)
            typed = [f"{c} — {k}" for c, k in typing.kind.items() if k != "текст"]
            if typed:
                rep.info(f"{f.name}: приведены типы — {', '.join(typed)}")
            if typing.failed:
                rep.warn(f"{f.name}: значений оставлено текстом — {len(typing.failed)} "
                         f"(например, «{typing.failed[0][2]}» в колонке «{typing.failed[0][1]}»)")
        sheets.append(Sheet(t.name.split(" / ")[-1] if " / " in t.name else "Данные", t.columns, t.rows))
    return [write_xlsx(out_dir / f"{f.stem}.xlsx", sheets)]
