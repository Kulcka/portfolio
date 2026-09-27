"""Проверка книги настоящим Excel через COM: пересчёт, сверка с зеркальной моделью, PDF.

Запуск:
    python -m finmodel.excel_check                 # демо: собрать, сверить, сохранить, PDF
    python -m finmodel.excel_check --config my.json --out my.xlsx

Что делает:
1. собирает книгу генератором (build.py);
2. открывает её в отдельном экземпляре Excel (DispatchEx), выполняет CalculateFull;
3. сравнивает каждую расчётную ячейку с зеркальной моделью (mirror.py), допуск 0,01;
4. то же для вариантов: три сценария, три налоговых режима, другие способ доставки
   и задержка выплат (меняет жёлтые ячейки через COM и пересчитывает);
5. возвращает исходные допущения, сохраняет книгу уже с посчитанными значениями
   и экспортирует лист «Дашборд» в PDF.
Excel закрывается в finally; если процесс не завершился сам — убивается только
тот EXCEL.EXE, который запустила проверка (по PID).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gc
import os
import subprocess
import sys
import time
from pathlib import Path

from openpyxl.utils import get_column_letter as CL

from . import build as B
from .config import FREIGHT_METHODS, HORIZON, SCENARIOS, TAX_MODES, Assumptions, demo_config
from .mirror import SCENARIO_TABLE_KEYS, compute, scenario_table, sensitivity

TOL = 0.01
EXCEL_EPOCH = dt.date(1899, 12, 30)
ROOT = Path(__file__).resolve().parent.parent


def _pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                         text=True, errors="replace").stdout
    return str(pid) in out


class ExcelSession:
    """Отдельный экземпляр Excel; гарантированно закрывается."""

    def __init__(self):
        self.app = None
        self.pid = None
        self.books = []

    def __enter__(self):
        import win32com.client
        import win32process
        self.app = win32com.client.DispatchEx("Excel.Application")
        _, self.pid = win32process.GetWindowThreadProcessId(self.app.Hwnd)
        self.app.Visible = False
        self.app.DisplayAlerts = False
        self.app.ScreenUpdating = False
        return self

    def open(self, path: Path):
        wb = self.app.Workbooks.Open(str(path.resolve()), UpdateLinks=0, ReadOnly=False)
        self.books.append(wb)
        return wb

    def __exit__(self, *exc):
        try:
            for wb in self.books:
                try:
                    wb.Close(SaveChanges=False)
                except Exception:
                    pass
            self.books.clear()
            gc.collect()                       # отпустить прокси книг до выхода Excel
            if self.app is not None:
                try:
                    self.app.Quit()
                except Exception:
                    pass
        finally:
            self.app = None
            gc.collect()
            if self.pid:
                for _ in range(40):
                    if not _pid_alive(self.pid):
                        break
                    time.sleep(0.25)
                else:
                    subprocess.run(["taskkill", "/PID", str(self.pid), "/F"],
                                   capture_output=True)
                    print(f"Excel (PID {self.pid}) не закрылся сам — завершён принудительно")
        return False


# ------------------------------------------------------------------ чтение листов
class SheetCache:
    def __init__(self, wb):
        self.wb = wb
        self.data = {}

    def val(self, sheet: str, row: int, col: int):
        if sheet not in self.data:
            ws = self.wb.Worksheets(sheet)
            last = ws.UsedRange.Row + ws.UsedRange.Rows.Count
            vals = ws.Range(f"A1:{CL(B.COL_START + HORIZON)}{last}").Value2
            self.data[sheet] = vals
        return self.data[sheet][row - 1][col - 1]

    def addr(self, sheet: str, addr: str):
        col = "".join(ch for ch in addr if ch.isalpha())
        row = int("".join(ch for ch in addr if ch.isdigit()))
        from openpyxl.utils import column_index_from_string
        return self.val(sheet, row, column_index_from_string(col))


def _norm(v):
    if isinstance(v, dt.date):
        return float((v - EXCEL_EPOCH).days)
    if isinstance(v, bool):
        return v
    return v


def _same(x, y) -> bool:
    x, y = _norm(x), _norm(y)
    if isinstance(y, bool) or isinstance(x, bool):
        return x == y
    if isinstance(y, str) or isinstance(x, str):
        return x == y
    if x is None:
        x = 0.0
    try:
        return abs(float(x) - float(y)) <= TOL
    except (TypeError, ValueError):
        return False


def compare(wb, L: B.Layout, r: dict, label: str, with_tables: bool = False,
            a: Assumptions | None = None) -> tuple[int, list[str]]:
    """Сверить книгу с результатом зеркальной модели. Возвращает (число ячеек, ошибки)."""
    sc = SheetCache(wb)
    errs: list[str] = []
    n = 0

    def chk(sheet, row, col, expected, what):
        nonlocal n
        n += 1
        got = sc.val(sheet, row, col)
        if not _same(got, expected):
            errs.append(f"[{label}] {sheet}!{CL(col)}{row} ({what}): Excel={got!r} "
                        f"Python={expected!r}")

    months = range(1, HORIZON + 1)
    for key, row in L.unit_rows.items():
        for k in range(L.n_skus):
            chk(B.SH_UNIT, row, L.unit_col0 + k, r["unit"][key][k], f"unit.{key}.{k}")
    for key, rows in L.flow_rows.items():
        for k, row in enumerate(rows):
            for t in months:
                chk(B.SH_FLOW, row, B.mcol(t), r["flow"][key][k][t - 1], f"flow.{key}.{k}")
    for key, row in L.flow_total_rows.items():
        for t in months:
            chk(B.SH_FLOW, row, B.mcol(t), r["flow_total"][key][t - 1], f"flow_total.{key}")
    for key, row in L.pnl_rows.items():
        if key.startswith("_"):
            continue
        for t in months:
            chk(B.SH_PNL, row, B.mcol(t), r["pnl"][key][t - 1], f"pnl.{key}")
    for key, rows in L.pnl_sku_rows.items():
        for k, row in enumerate(rows):
            for t in months:
                chk(B.SH_PNL, row, B.mcol(t), r["pnl_sku"][key][k][t - 1], f"pnl.{key}.{k}")
    for key, row in L.cash_rows.items():
        if key.startswith("_") or key in ("lag", "frac"):
            continue
        for t in months:
            chk(B.SH_CASH, row, B.mcol(t), r["cash"][key][t - 1], f"cash.{key}")
    for key, rows in L.cash_sku_rows.items():
        for k, row in enumerate(rows):
            for t in months:
                chk(B.SH_CASH, row, B.mcol(t), r["cash_sku"][key][k][t - 1], f"cash.{key}.{k}")
    for key in ("lag", "frac"):
        n += 1
        got = sc.addr(B.SH_CASH, L.cash_cells[key])
        if not _same(got, r["cash"][key]):
            errs.append(f"[{label}] cash.{key}: Excel={got!r} Python={r['cash'][key]!r}")
    for key, addr in L.end_cells.items():
        n += 1
        got = sc.addr(B.SH_CASH, addr)
        if not _same(got, r["end"][key]):
            errs.append(f"[{label}] end.{key}: Excel={got!r} Python={r['end'][key]!r}")
    for i, row in enumerate(L.annual_rows):
        chk(B.SH_PNL, row, B.COL_TOTAL, r["kpi"]["annual_revenue"][i], f"annual.{i}")
    for key, nm in L.kpi_names.items():
        n += 1
        sheet, addr = L.names[nm]
        got = sc.addr(sheet, addr.replace("$", ""))
        if not _same(got, r["kpi"][key]):
            errs.append(f"[{label}] kpi.{key} ({nm}): Excel={got!r} Python={r['kpi'][key]!r}")
    # таблица товаров на дашборде
    for k in range(L.n_skus):
        row = L.dash_sku_row0 + k
        for j, (src, key) in enumerate([("unit", "price"), ("unit", "landed"),
                                        ("unit", "profit"), ("unit", "margin"),
                                        ("unit", "roi"), ("unit", "bep"),
                                        ("unit", "plateau_eff"), ("kpi", "sku_sold"),
                                        ("kpi", "sku_revenue"), ("kpi", "sku_lost")]):
            exp = r["unit"][key][k] if src == "unit" else r["kpi"][key][k]
            chk(B.SH_DASH, row, 4 + j, exp, f"dash.{key}.{k}")

    if with_tables and a is not None:
        for kind in ("price", "sales", "peak"):
            grid = sensitivity(a, kind)
            r0, c0 = L.sens[kind]
            for i, line in enumerate(grid):
                for j, v in enumerate(line):
                    chk(B.SH_SENS, r0 + i, c0 + j, v, f"sens.{kind}[{i},{j}]")
        rows = scenario_table(a)
        r0, c0 = L.scen_table
        for i, line in enumerate(rows):
            for j, v in enumerate(line):
                chk(B.SH_SC, r0 + i, c0 + j, v, f"scen_table.{SCENARIO_TABLE_KEYS[j]}[{i}]")
    return n, errs


def set_input(wb, L: B.Layout, name: str, value):
    sheet, addr = L.names[name]
    wb.Worksheets(sheet).Range(addr.replace("$", "")).Value = value


def variants(a: Assumptions):
    """(подпись, {имя ячейки: значение}, изменённые допущения, номер сценария)."""
    out = []
    for sc in SCENARIOS[1:]:
        out.append((f"сценарий «{sc}»", {"Сценарий_выбор": sc}, a.copy(active_scenario=sc)))
    for mode in TAX_MODES:
        if mode != a.tax_mode:
            out.append((f"налог {mode}", {"Налог_режим": mode}, a.copy(tax_mode=mode)))
    other = [m for m in FREIGHT_METHODS if m != a.freight_method][0]
    out.append((f"доставка «{other}», задержка 45 дн., пессимист., УСН 15%",
                {"Доставка_способ": other, "Задержка_выплат_дн": 45,
                 "Сценарий_выбор": SCENARIOS[1], "Налог_режим": TAX_MODES[1]},
                a.copy(freight_method=other, payout_delay_days=45,
                       active_scenario=SCENARIOS[1], tax_mode=TAX_MODES[1])))
    out.append(("срок поставки 3 мес., страховой запас 0, задержка 0",
                {"Срок_поставки": 3, "Страх_запас": 0, "Задержка_выплат_дн": 0},
                a.copy(lead_time_months=3, safety_months=0, payout_delay_days=0)))
    return out


def export_png(xl, wb, L: B.Layout, out_dir: Path, report: dict):
    """Картинки листов для портфолио (через копию диапазона как рисунка).
    Книга уже сохранена; временный лист добавляется и не сохраняется."""
    out_dir.mkdir(parents=True, exist_ok=True)
    unit_last = max(L.unit_rows.values())
    cash_last = L.cash_rows["cum_cf"]
    scen_last = L.scen_table[0] + 2
    targets = [
        ("dashboard.png", B.SH_DASH, None),
        ("unit-economics.png", B.SH_UNIT, f"A1:{CL(2 + L.n_skus)}{unit_last}"),
        ("cash-flow.png", B.SH_CASH, f"A1:{CL(B.mcol(12))}{cash_last}"),
        ("scenarios.png", B.SH_SC, f"A1:I{scen_last}"),
    ]
    tmp = wb.Worksheets.Add()
    report["png"] = []
    try:
        for fname, sheet, addr in targets:
            ws = wb.Worksheets(sheet)
            rng = ws.Range(addr or ws.PageSetup.PrintArea)
            path = out_dir / fname
            for visible in (False, True):
                xl.app.Visible = visible
                xl.app.ScreenUpdating = True
                ws.Activate()
                rng.CopyPicture(1, 2)          # как на экране, растр
                time.sleep(0.8)
                co = tmp.ChartObjects().Add(0, 0, rng.Width, rng.Height)
                co.Activate()
                time.sleep(0.5)
                co.Chart.Paste()
                time.sleep(0.8)
                if path.exists():
                    path.unlink()
                co.Chart.Export(str(path.resolve()))
                co.Delete()
                co = None
                if path.exists() and path.stat().st_size > 5000:
                    break
            report["png"].append(str(path))
            ws = rng = None
    finally:
        xl.app.Visible = False
        tmp = None


def _run_in_excel(xl, a, L, xlsx, pdf, save, quick, report, png_dir):
    """Работа внутри открытого Excel. Ссылки на COM-объекты живут только здесь:
    после возврата они освобождаются, и Excel закрывается сам."""
    wb = xl.open(xlsx)
    xl.app.CalculateFull()
    n, errs = compare(wb, L, compute(a), "база", with_tables=True, a=a)
    report["checked"] += n
    report["errors"] += errs
    report["variants"].append(("база + таблицы данных", n, len(errs)))
    if not quick:
        base_inputs = {nm: xl_get(wb, L, nm) for nm in
                       ("Сценарий_выбор", "Налог_режим", "Доставка_способ",
                        "Задержка_выплат_дн", "Срок_поставки", "Страх_запас")}
        for label, cells, a2 in variants(a):
            for nm, v in cells.items():
                set_input(wb, L, nm, v)
            xl.app.Calculate()
            n, errs = compare(wb, L, compute(a2), label)
            report["checked"] += n
            report["errors"] += errs
            report["variants"].append((label, n, len(errs)))
            for nm, v in base_inputs.items():
                set_input(wb, L, nm, v)
        xl.app.CalculateFull()
        n, errs = compare(wb, L, compute(a), "база после возврата")
        report["checked"] += n
        report["errors"] += errs
    if save:
        ws = wb.Worksheets(B.SH_DASH)
        ws.Activate()
        ws.Range("A1").Select()
        wb.Save()
        if pdf is not None:
            pdf.parent.mkdir(parents=True, exist_ok=True)
            if pdf.exists():
                pdf.unlink()
            ws.ExportAsFixedFormat(0, str(pdf.resolve()), 0, True, False)
            report["pdf"] = str(pdf)
        if png_dir is not None:
            export_png(xl, wb, L, png_dir, report)


def run(a: Assumptions, xlsx: Path, pdf: Path | None = None, save: bool = True,
        quick: bool = False, png_dir: Path | None = None) -> dict:
    """Собрать книгу, сверить в Excel, сохранить с посчитанными значениями, PDF."""
    wb_py, L = B.build_workbook(a)
    xlsx.parent.mkdir(parents=True, exist_ok=True)
    wb_py.save(xlsx)
    report = {"checked": 0, "errors": [], "variants": []}
    with ExcelSession() as xl:
        _run_in_excel(xl, a, L, xlsx, pdf, save, quick, report, png_dir)
    report["excel_pid_closed"] = xl.pid is not None and not _pid_alive(xl.pid)
    return report


def xl_get(wb, L, name):
    sheet, addr = L.names[name]
    return wb.Worksheets(sheet).Range(addr.replace("$", "")).Value


def main(argv=None):
    ap = argparse.ArgumentParser(description="Сверка книги через Excel COM и экспорт PDF")
    ap.add_argument("--config", help="JSON с допущениями (по умолчанию — демо)")
    ap.add_argument("--out", default=str(ROOT / "examples" / "finmodel-import-china.xlsx"))
    ap.add_argument("--pdf", default=str(ROOT / "examples" / "finmodel-dashboard.pdf"))
    ap.add_argument("--png-dir", default=str(ROOT / "examples" / "screenshots"),
                    help="куда сохранить картинки листов для портфолио ('' — не сохранять)")
    args = ap.parse_args(argv)
    from .config import load_config
    a = load_config(args.config) if args.config else demo_config()
    rep = run(a, Path(args.out), Path(args.pdf),
              png_dir=Path(args.png_dir) if args.png_dir else None)
    for label, n, e in rep["variants"]:
        print(f"  {label}: ячеек {n}, расхождений {e}")
    print(f"Всего сверено ячеек: {rep['checked']}, расхождений > {TOL}: {len(rep['errors'])}")
    for line in rep["errors"][:40]:
        print("  " + line)
    print(f"Excel закрыт: {rep['excel_pid_closed']}")
    if rep.get("pdf"):
        print(f"PDF: {rep['pdf']}")
    for path in rep.get("png", []):
        print(f"PNG: {path}")
    return 1 if rep["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
