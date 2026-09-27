"""Настоящий пересчёт в Excel через COM и сверка с зеркальной моделью (метка excel).

Запуск только этих тестов:  pytest -m excel
Без Excel на машине:        pytest -m "not excel"
"""
import subprocess

import pytest

from conftest import simple_config

win32com = pytest.importorskip("win32com.client")
pytestmark = pytest.mark.excel


def excel_pids() -> set[str]:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq EXCEL.EXE", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, errors="replace").stdout
    return {line.split(",")[1].strip('"') for line in out.splitlines() if "EXCEL" in line.upper()}


def test_demo_full_check_in_excel(demo, tmp_path):
    from finmodel.excel_check import run
    before = excel_pids()
    rep = run(demo, tmp_path / "demo.xlsx", tmp_path / "demo.pdf", save=True)
    assert rep["errors"] == [], rep["errors"][:10]
    assert rep["checked"] > 20000
    assert len(rep["variants"]) == 7
    assert (tmp_path / "demo.pdf").stat().st_size > 50_000
    assert rep["excel_pid_closed"]
    assert excel_pids() <= before           # не оставили висящих EXCEL.EXE


def test_other_config_in_excel(tmp_path):
    """Другой набор допущений: 1 товар, начальный остаток, УСН 15 %, доставка по объёму."""
    from finmodel.excel_check import run
    a = simple_config(tax_mode="УСН 15%", freight_method="по объёму", payout_delay_days=21,
                      lead_time_months=2, safety_months=1, start_capital=300_000)
    a.skus[0].start_stock = 150
    a.skus[0].returns = 0.12
    rep = run(a, tmp_path / "simple.xlsx", None, save=False, quick=True)
    assert rep["errors"] == [], rep["errors"][:10]
    assert rep["excel_pid_closed"]
