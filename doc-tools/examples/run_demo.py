"""Демо целиком: генерирует входные файлы в examples/input и прогоняет все команды в examples/output.

Запуск из папки проекта:
    .venv\\Scripts\\python.exe examples\\run_demo.py            # всё, включая PDF через Word
    .venv\\Scripts\\python.exe examples\\run_demo.py --no-word  # без Microsoft Word
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from doctools.cli import main as doctools  # noqa: E402
from doctools.samples import generate  # noqa: E402

INP = HERE / "input"
OUT = HERE / "output"


def run(*args: str) -> None:
    shown = " ".join(f'"{a}"' if " " in a or "{" in a else a for a in args)
    print(f"\n$ doctools {shown}")
    code = doctools(list(args))
    if code != 0:
        raise SystemExit(f"Команда завершилась с кодом {code}")


def rel(p: Path) -> str:
    """Путь относительно папки проекта — так команды в выводе можно скопировать и повторить."""
    return str(Path(p).resolve().relative_to(HERE.parent))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-word", action="store_true", help="не использовать Microsoft Word (без PDF из DOCX)")
    a = ap.parse_args()

    os.chdir(HERE.parent)
    if OUT.exists():
        shutil.rmtree(OUT)
    s = generate(INP)
    print(f"Входные файлы: {INP}")

    o1, o2, o3, o4, o5, o6 = (OUT / d for d in ("1_pdf2xlsx", "2_pdf2docx", "3_merge", "4_clean", "5_docx-fill", "6_convert"))
    run("pdf2xlsx", rel(s.price_pdf), "-o", rel(o1 / "прайс_СеверСнаб.xlsx"))
    run("pdf2xlsx", rel(s.statement_pdf), "-o", rel(o1 / "выписка_сентябрь.xlsx"))
    run("pdf2xlsx", rel(s.scan_pdf), "-o", rel(o1 / "скан_договора.xlsx"))

    run("pdf2docx", rel(s.price_pdf), "-o", rel(o2 / "прайс_СеверСнаб.docx"))
    run("pdftext", rel(s.statement_pdf), "-o", rel(o2 / "выписка_сентябрь.txt"))
    run("pdf2docx", rel(s.scan_pdf), "-o", rel(o2 / "скан_договора.docx"))

    merged = o3 / "клиенты_объединённые.xlsx"
    run("merge", rel(s.sales_xlsx), rel(s.crm_xlsx), rel(s.site_csv), "-o", rel(merged))

    clean = o4 / "клиенты_чистые.xlsx"
    run("clean", rel(merged), "-o", rel(clean), "--dedup-keys", "ФИО,Телефон", "--fuzzy-keys", "ФИО")

    fill = ["docx-fill", rel(s.letter_docx), rel(s.recipients_xlsx), "-o", rel(o5 / "письма"),
            "--name", "Письмо_{{Номер договора}}_{{Компания}}"]
    run(*(fill + ([] if a.no_word else ["--pdf"])))

    run("convert", rel(clean), "--sheet", "Результат", "--to", "csv", "-o", rel(o6 / "csv"))
    run("convert", rel(clean), "--sheet", "Результат", "--to", "json", "-o", rel(o6 / "json"))
    run("convert", rel(s.site_csv), "--to", "xlsx", "-o", rel(o6 / "xlsx"))
    if not a.no_word:
        run("convert", rel(s.letter_docx), "--to", "pdf", "-o", rel(o6 / "pdf"))

    print(f"\nГотово. Результаты: {OUT}")


if __name__ == "__main__":
    main()
