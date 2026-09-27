"""pdf2xlsx: таблицы из текстовых PDF в Excel.

Что умеет:
- находит таблицы на каждой странице (по линиям сетки; если сетки нет —
  по линиям строк и выравниванию текста);
- склеивает таблицу, разорванную между страницами (с повтором шапки и без);
- строки-разделы («Крепёж», «Инструмент» на всю ширину) превращает в колонку «Раздел»;
- приводит числа («1 250,00») и даты к типам Excel, нераспознанное перечисляет в отчёте;
- страницы без текстового слоя (сканы) помечает «нужен OCR».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber

from . import DocToolsError
from .normalize import clean_spaces, norm_key, parse_date, parse_number
from .report import Report
from .tableio import PROBLEM_FILL, Sheet, Table, infer_and_convert, unique_names, write_xlsx

LINES = {"vertical_strategy": "lines", "horizontal_strategy": "lines"}
ROW_LINES = {"vertical_strategy": "text", "horizontal_strategy": "lines", "min_words_vertical": 2}
TEXT = {"vertical_strategy": "text", "horizontal_strategy": "text"}
STRATEGIES = {"lines": [LINES], "rows": [ROW_LINES], "text": [TEXT], "auto": [LINES, ROW_LINES]}
STRATEGY_NAMES = {id(LINES): "сетка", id(ROW_LINES): "линии строк + колонки по просветам в тексте", id(TEXT): "выравнивание текста"}


@dataclass
class RawTable:
    page: int
    top: float
    bottom: float
    page_height: float
    rows: list[list[str | None]]
    strategy: str
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)
    xs: list[float] | None = None  # границы колонок, если найдены по просветам


@dataclass
class LogicalTable:
    header: list[str] | None
    rows: list[list[str | None]] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)       # страница каждой строки
    first_page: int = 0
    last_page: int = 0
    last_bottom_share: float = 0.0
    strategy: str = ""
    joins: list[str] = field(default_factory=list)


@dataclass
class PdfScan:
    tables: list[RawTable]
    ocr_pages: list[int]
    empty_pages: list[int]
    page_count: int


def parse_pages(spec: str | None, total: int) -> list[int]:
    """«1-3,5» -> [1, 2, 3, 5] (номера с единицы)."""
    if not spec:
        return list(range(1, total + 1))
    pages: set[int] = set()
    try:
        for part in spec.replace(" ", "").split(","):
            if "-" in part:
                a, b = part.split("-", 1)
                pages.update(range(int(a), int(b or total) + 1))
            elif part:
                pages.add(int(part))
    except ValueError as exc:
        raise DocToolsError(f"Не понял номера страниц «{spec}». Пример: 1-3,5") from exc
    bad = sorted(p for p in pages if not 1 <= p <= total)
    if bad:
        raise DocToolsError(f"В файле {total} стр., а указаны: {', '.join(map(str, bad))}")
    return sorted(pages)


def page_has_text(page: Any) -> bool:
    return len(page.chars) >= 5


def open_pdf(path: Path) -> pdfplumber.PDF:
    path = Path(path)
    if not path.exists():
        raise DocToolsError(f"Файл не найден: {path}")
    try:
        return pdfplumber.open(path)
    except Exception as exc:
        msg = str(exc).lower()
        if "password" in msg or "encrypt" in msg:
            raise DocToolsError(f"{path.name}: PDF защищён паролем — пришлите файл без пароля") from exc
        raise DocToolsError(f"{path.name}: не удалось открыть PDF ({exc})") from exc


def scan_pdf(path: Path, strategy: str = "auto", pages: str | None = None) -> PdfScan:
    if strategy not in STRATEGIES:
        raise DocToolsError(f"Неизвестная стратегия «{strategy}». Варианты: {', '.join(STRATEGIES)}")
    raw: list[RawTable] = []
    ocr, empty = [], []
    hint: list[float] | None = None
    with open_pdf(path) as pdf:
        total = len(pdf.pages)
        for pno in parse_pages(pages, total):
            page = pdf.pages[pno - 1]
            if not page_has_text(page):
                (ocr if page.images else empty).append(pno)
                continue
            found = find_page_tables(page, pno, strategy, hint)
            hint = found[-1].xs if found else None
            raw += found
    return PdfScan(raw, ocr, empty, total)


def find_page_tables(page: Any, pno: int, strategy: str = "auto", hint: list[float] | None = None) -> list[RawTable]:
    """Таблицы одной страницы: пробуем способы по очереди, берём первый, что что-то нашёл.

    hint — границы колонок таблицы с прошлой страницы (для продолжения без шапки).
    """
    out: list[RawTable] = []
    for settings in STRATEGIES[strategy]:
        found = [t for t in page.find_tables(settings) if _usable(t)]
        for t in found:
            cells = t.extract()
            xs: list[float] | None = None
            if settings is ROW_LINES:
                res = _extract_by_gutters(page, t, hint)
                if res:
                    cells, xs, (xs_top, xs_bottom) = res
            rows = _drop_empty([[_clean_cell(c) for c in r] for r in cells], keep_columns=xs is not None)
            if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
                bbox = tuple(t.bbox)
                if xs is not None:  # шапка/хвост над и под линиями — тоже часть таблицы
                    bbox = (min(bbox[0], xs[0]), min(bbox[1], xs_top), max(bbox[2], xs[-1]), max(bbox[3], xs_bottom))
                out.append(RawTable(pno, bbox[1], bbox[3], float(page.height), rows,
                                    STRATEGY_NAMES[id(settings)], bbox, xs))
        if out:
            break
    return out


def _adjacent_text_edge(page: Any, x0: float, x1: float, edge: float, above: bool) -> float | None:
    """Строки текста вплотную к линии edge (сверху или снизу) в пределах ширины таблицы.

    Возвращает дальнюю границу этих строк или None. «Вплотную» — зазор меньше
    высоты строки; следующая строка присоединяется, только если стоит ещё плотнее
    (шапка в две строки), обычный текст до/после таблицы не захватывается.
    """
    groups: dict[int, list[dict]] = {}
    for c in page.chars:
        if not (c["x0"] >= x0 - 1 and c["x1"] <= x1 + 1 and c["text"].strip()):
            continue
        if above and edge - 60 <= c["bottom"] <= edge + 1:
            groups.setdefault(round(c["bottom"]), []).append(c)
        elif not above and edge - 1 <= c["top"] <= edge + 60:
            groups.setdefault(round(c["top"]), []).append(c)
    cur, found = edge, None
    for key in sorted(groups, reverse=above):
        grp = groups[key]
        g_top, g_bottom = min(c["top"] for c in grp), max(c["bottom"] for c in grp)
        gap = cur - g_bottom if above else g_top - cur
        if gap > (g_bottom - g_top) * (0.9 if found is None else 0.5):
            break
        cur = found = g_top if above else g_bottom
    return found


def _extract_by_gutters(page: Any, t: Any, hint: list[float] | None = None,
                        min_gap: float = 2.0) -> tuple[list[list[str | None]], list[float], tuple[float, float]] | None:
    """Колонки таблицы без вертикальных линий — по «просветам» между текстом.

    Берём все символы таблицы (вместе со строкой шапки, если она стоит сразу над
    первой линией), объединяем их горизонтальные отрезки; промежутки, где нет ни
    одного символа ни в одной строке, — границы колонок. Так перенос длинного
    текста внутри ячейки не рвёт колонку (в отличие от выравнивания по краям
    слов). Отрезки без подписи в шапке присоединяются к соседнему слева — это
    ложный просвет внутри колонки (например, пробел в «14 850,15»).
    """
    x0, top, x1, bottom = t.bbox
    bands = [(r.bbox[1], r.bbox[3]) for r in t.rows]
    if not bands:
        return None

    def in_x(c: dict) -> bool:
        return c["x0"] >= x0 - 1 and c["x1"] <= x1 + 1 and bool(c["text"].strip())

    # шапка без линии сверху и последняя строка без линии снизу: строки текста вплотную к таблице
    head_top = _adjacent_text_edge(page, x0, x1, top, above=True)
    tail_bottom = _adjacent_text_edge(page, x0, x1, bottom, above=False)
    if tail_bottom is not None:
        bands.append((bottom, tail_bottom + 0.5))
        bottom = tail_bottom + 0.5
    has_header = head_top is not None
    if has_header:
        bands.insert(0, (head_top - 0.5, top))
    else:
        first = page.crop((x0, bands[0][0], x1, bands[0][1]), strict=False).extract_words()
        has_header = bool(first) and all(parse_number(w["text"]) is None and parse_date(w["text"]) is None for w in first)

    chars = [c for c in page.chars if in_x(c) and bands[0][0] <= c["top"] and c["bottom"] <= bottom + 1]
    spans: list[list[float]] = []
    for c in sorted(chars, key=lambda c: c["x0"]):
        if spans and c["x0"] - spans[-1][1] < min_gap:
            spans[-1][1] = max(spans[-1][1], c["x1"])
        else:
            spans.append([c["x0"], c["x1"]])
    if len(spans) < 2:
        return None
    if has_header:
        h_top, h_bottom = bands[0]
        head = [c for c in chars if h_top - 1 <= c["top"] and c["bottom"] <= h_bottom + 1]
        labelled = [any(a <= (c["x0"] + c["x1"]) / 2 <= b for c in head) for a, b in spans]
        merged: list[list[float]] = []
        for span, has in zip(spans, labelled):
            if merged and not has:
                merged[-1][1] = span[1]
            else:
                merged.append(list(span))
        spans = merged
    elif hint and hint[0] <= spans[0][0] + 2 and spans[-1][1] <= hint[-1] + 2:
        spans = [[a, b] for a, b in zip(hint, hint[1:])]  # продолжение без шапки: колонки как на прошлой странице
        spans[0][0] += 1
        spans[-1][1] -= 1
    xs = [spans[0][0] - 1] + [(a[1] + b[0]) / 2 for a, b in zip(spans, spans[1:])] + [spans[-1][1] + 1]
    rows = []
    for b_top, b_bottom in bands:
        row = []
        for cx0, cx1 in zip(xs, xs[1:]):
            text = page.crop((cx0, b_top, cx1, b_bottom), strict=False).extract_text()
            row.append(text or None)
        rows.append(row)
    return rows, xs, (bands[0][0], bands[-1][1])


def _usable(t: Any) -> bool:
    cells = t.extract()
    flat = [c for r in cells for c in r]
    filled = [c for c in flat if c and c.strip()]
    return len(cells) >= 2 and len(cells[0]) >= 2 and len(filled) >= 0.3 * len(flat)


def _clean_cell(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.replace("-\n", "\u0000").replace("\n", " ")
    # перенос слова по слогам внутри ячейки: «крепёж-\nный» -> «крепёжный»
    v = v.replace("\u0000", "")
    v = clean_spaces(v)
    return v or None


def _drop_empty(rows: list[list[str | None]], keep_columns: bool = False) -> list[list[str | None]]:
    rows = [r for r in rows if any(c for c in r)]
    if not rows or keep_columns:
        return rows
    keep = [i for i in range(max(len(r) for r in rows)) if any(i < len(r) and r[i] for r in rows)]
    return [[r[i] if i < len(r) else None for i in keep] for r in rows]


def _is_header(row: list[str | None]) -> bool:
    vals = [c for c in row if c]
    if len(vals) < max(2, len(row) // 2):
        return False
    return all(parse_number(v) is None and parse_date(v) is None for v in vals)


def _same_header(a: list[str] | None, b: list[str | None]) -> bool:
    return a is not None and len(a) == len(b) and [norm_key(x or "") for x in a] == [norm_key(x or "") for x in b]


def assemble(raw: list[RawTable]) -> list[LogicalTable]:
    """Склейка фрагментов таблиц по страницам в логические таблицы."""
    out: list[LogicalTable] = []
    cur: LogicalTable | None = None
    for t in raw:
        first = t.rows[0]
        ncols = len(first)
        cont = False
        if cur is not None and t.page == cur.last_page + 1 and cur.header is not None and len(cur.header) == ncols:
            if _same_header(cur.header, first):
                cont, body = True, t.rows[1:]
                cur.joins.append(f"стр. {t.page}: продолжение, повтор шапки убран")
            elif not _is_header(first) and cur.last_bottom_share > 0.7 and t.top / t.page_height < 0.3:
                cont, body = True, t.rows
                cur.joins.append(f"стр. {t.page}: продолжение без шапки")
        if not cont:
            header = [c or f"Колонка {i + 1}" for i, c in enumerate(first)] if _is_header(first) else None
            body = t.rows[1:] if header else t.rows
            cur = LogicalTable(header=header or [f"Колонка {i + 1}" for i in range(ncols)],
                               first_page=t.page, strategy=t.strategy)
            if header is None:
                cur.joins.append("шапка таблицы не найдена — колонки названы по номерам")
            out.append(cur)
        cur.rows += body
        cur.pages += [t.page] * len(body)
        cur.last_page = t.page
        cur.last_bottom_share = t.bottom / t.page_height
    return out


def _sections(lt: LogicalTable) -> int:
    """Строки-разделы (одна заполненная ячейка на всю ширину) -> колонка «Раздел»."""
    if len(lt.header or []) < 3:
        return 0
    section: str | None = None
    rows, pages, found = [], [], 0
    for r, p in zip(lt.rows, lt.pages):
        vals = [c for c in r if c]
        if len(vals) == 1 and r[0] and parse_number(r[0]) is None:
            section = r[0]
            found += 1
            continue
        rows.append([section] + r)
        pages.append(p)
    if found:
        lt.header = ["Раздел"] + list(lt.header or [])
        lt.rows, lt.pages = rows, pages
    return found


def pdf_to_xlsx(src: Path, dst: Path | None = None, *, strategy: str = "auto",
                pages: str | None = None, source_page: bool = False) -> tuple[Path, Report]:
    src = Path(src)
    dst = Path(dst) if dst else src.with_suffix(".xlsx")
    rep = Report(f"PDF -> Excel: {src.name}")
    scan = scan_pdf(src, strategy, pages)
    logical = assemble(scan.tables)

    sheets: list[Sheet] = []
    failed_rows: list[list[Any]] = []
    total_rows = 0
    for n, lt in enumerate(logical, start=1):
        sec = _sections(lt)
        columns = unique_names(lt.header or [])
        table = Table(columns, [list(r) + [None] * (len(columns) - len(r)) for r in lt.rows])
        typing = infer_and_convert(table)
        if source_page:
            table.columns.append("Стр. PDF")
            for row, p in zip(table.rows, lt.pages):
                row.append(p)
        span = f"стр. {lt.first_page}" if lt.first_page == lt.last_page else f"стр. {lt.first_page}-{lt.last_page}"
        title = f"Таблица {n} ({span})"
        fills = {}
        for ri, col, val in typing.failed:
            fills[(ri, table.columns.index(col))] = PROBLEM_FILL
            failed_rows.append([title, lt.pages[ri], ri + 2, col, val, f"оставлено текстом (колонка — {typing.kind[col]})"])
        sheets.append(Sheet(title, table.columns, table.rows, fills))
        total_rows += len(table.rows)
        typed = [f"{c} — {k}" for c, k in typing.kind.items() if k != "текст"]
        rep.info(f"{title}: {len(table.rows)} строк × {len(columns)} колонок, способ поиска: {lt.strategy}"
                 + (f"; склейка: {'; '.join(lt.joins)}" if lt.joins else "")
                 + (f"; строк-разделов: {sec} (вынесены в колонку «Раздел»)" if sec else ""))
        if typed:
            rep.info(f"{title}: приведены типы — {', '.join(typed)}")
        totals = [ri for ri, r in enumerate(table.rows) if any(isinstance(v, str) and v.lower().startswith(("итого", "всего", "обороты")) for v in r)]
        if totals:
            rep.info(f"{title}: строки итогов оставлены как в документе (строк: {len(totals)})")

    if scan.ocr_pages:
        rep.warn(f"Страницы без текстового слоя (скан/картинка): {', '.join(map(str, scan.ocr_pages))} — "
                 "таблицы с них не извлечены, нужен OCR (отдельная услуга, по согласованию)")
    if scan.empty_pages:
        rep.info(f"Пустые страницы: {', '.join(map(str, scan.empty_pages))}")
    no_tables = sorted(set(parse_pages(pages, scan.page_count)) - {t.page for t in scan.tables}
                       - set(scan.ocr_pages) - set(scan.empty_pages))
    if no_tables:
        rep.info(f"Страницы с текстом, но без таблиц: {', '.join(map(str, no_tables))}")
    if failed_rows:
        rep.warn(f"Не распознано как число/дата значений: {len(failed_rows)} — оставлены текстом и подсвечены в Excel")
        rep.table("Нераспознанные значения", ["Таблица", "Стр. PDF", "Строка Excel", "Колонка", "Значение", "Что сделано"], failed_rows)
    if not logical:
        if scan.ocr_pages and len(scan.ocr_pages) == scan.page_count:
            rep.error("В документе нет текстового слоя — это скан. Без OCR таблицы не извлечь.")
        else:
            rep.error("Таблиц не найдено. Если таблица есть, попробуйте --strategy text или пришлите файл на разбор.")

    rep.stat("Страниц в PDF", scan.page_count)
    rep.stat("Таблиц найдено", len(logical))
    rep.stat("Строк данных", total_rows)
    rep.stat("Страниц «нужен OCR»", len(scan.ocr_pages))
    sheets.append(Sheet("Отчёт", [], rep.sheet_rows(), plain=True))
    write_xlsx(dst, sheets)
    rep.output(dst)
    return dst, rep
