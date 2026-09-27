"""Зеркальная модель: юнит-экономика вручную, склад, денежный поток, разрыв, сценарии, налоги."""
import datetime as dt

import pytest

from conftest import simple_config
from finmodel.config import SCENARIOS, TAX_MODES
from finmodel.mirror import compute, sensitivity, xl_round, xl_roundup


def test_excel_rounding():
    assert xl_round(2.5) == 3 and xl_round(-2.5) == -3
    assert xl_round(150 * 0.35) == 53          # 52.49999999999999 в двоичном виде
    assert xl_round(2.675, 2) == 2.68          # как ROUND в Excel, не как round() в Python
    assert xl_roundup(1.01) == 2 and xl_roundup(2.0) == 2


def test_unit_economics_by_hand():
    r = compute(simple_config())
    u = {k: v[0] for k, v in r["unit"].items()}
    # товар 100 ¥ × 10 = 1000; доставка 1 кг × 200 = 200; тамож. стоимость 1200
    assert u["goods"] == pytest.approx(1000)
    assert u["freight"] == pytest.approx(200)
    assert u["duty"] == pytest.approx(120)                  # 10 % от 1200
    assert u["vat"] == pytest.approx(264)                   # 20 % от (1200 + 120)
    assert u["customs_fee"] == pytest.approx(50)            # 5000 / партия 100
    assert u["landed"] == pytest.approx(1000 + 200 + 120 + 264 + 50 + 50 + 50)  # 1734
    assert u["commission"] == pytest.approx(1000)           # 20 % от 5000
    assert u["ads"] == pytest.approx(500)
    assert u["tax"] == pytest.approx(300)                   # УСН 6 % от цены
    assert u["profit"] == pytest.approx(5000 - 1734 - 1000 - 100 - 500 - 300)  # 1366
    assert u["margin"] == pytest.approx(1366 / 5000)
    assert u["markup"] == pytest.approx((5000 - 1734) / 1734)
    assert u["cert_payback"] == pytest.approx(10000 / 1366)


def test_freight_methods_and_returns():
    a = simple_config()
    by_vol = compute(a.copy(freight_method="по объёму"))["unit"]["freight"][0]
    by_max = compute(a.copy(freight_method="по большему"))["unit"]["freight"][0]
    assert by_vol == pytest.approx(0.01 * 20000) and by_max == pytest.approx(200)
    a2 = a.copy(reverse_logistics=50)
    a2.skus[0].returns = 0.2
    # (100 + 0,2 × 50) / 0,8 = 137,5 ₽ логистики на выкупленную единицу
    assert compute(a2)["unit"]["mp_logistics"][0] == pytest.approx(137.5)


@pytest.mark.parametrize("sc", [1, 2, 3])
def test_inventory_invariants(demo, sc):
    r = compute(demo, scenario=sc)
    L = demo.lead_time_months
    for k, s in enumerate(demo.skus):
        f = {key: r["flow"][key][k] for key in r["flow"]}
        prev = s.start_stock
        for i in range(18):
            assert f["stock"][i] >= 0
            assert f["sales"][i] <= f["demand"][i]
            assert f["stock"][i] == prev + f["arrivals"][i] - f["sales"][i]
            assert f["orders"][i] % s.batch == 0
            assert f["arrivals"][i] == (f["orders"][i - L] if i >= L else 0)
            prev = f["stock"][i]


def test_first_months_no_sales_until_goods_arrive():
    a = simple_config(lead_time_months=2)
    r = compute(a)
    assert r["flow"]["sales"][0][:2] == [0, 0]          # товар ещё едет
    assert r["flow"]["lost"][0][:2] == [100, 100]       # спрос есть — продать нечего
    assert r["flow"]["sales"][0][2] == 100


def test_cash_gap_detected_and_absent():
    poor = compute(simple_config(start_capital=100_000))
    assert poor["kpi"]["max_gap"] > 0
    assert poor["kpi"]["first_gap"] == dt.date(2027, 1, 1)   # заказ оплачен в 1-м месяце
    assert poor["kpi"]["max_gap"] == pytest.approx(-min(poor["cash"]["closing"]))
    rich = compute(simple_config(start_capital=10_000_000))
    assert rich["kpi"]["max_gap"] == 0 and rich["kpi"]["first_gap"] == "нет"
    # потребность в деньгах не зависит от стартового капитала
    assert rich["kpi"]["peak_investment"] == pytest.approx(poor["kpi"]["peak_investment"])


def test_payout_delay_split():
    a = simple_config(payout_delay_days=45)
    r = compute(a)
    base = r["cash"]["payout_base"]
    pay = r["cash"]["payout"]
    assert r["cash"]["lag"] == 1 and r["cash"]["frac"] == pytest.approx(0.5)
    for t in range(3, 18):
        assert pay[t] == pytest.approx(0.5 * base[t - 1] + 0.5 * base[t - 2])
    assert sum(base) - sum(pay) == pytest.approx(r["end"]["receivable"])
    r0 = compute(simple_config(payout_delay_days=0))
    assert r0["cash"]["payout"] == pytest.approx(r0["cash"]["payout_base"])


@pytest.mark.parametrize("mode", TAX_MODES)
@pytest.mark.parametrize("sc", [1, 2, 3])
def test_balance_identity(demo, mode, sc):
    a = demo.copy(tax_mode=mode)
    a.skus[0].start_stock = 120
    r = compute(a, scenario=sc)
    assert r["end"]["balance_diff"] == pytest.approx(0, abs=1e-6)
    assert r["end"]["balance_ok"] == "OK"


def test_taxes():
    a = simple_config()
    r6 = compute(a)
    assert sum(r6["pnl"]["tax"]) == pytest.approx(0.06 * sum(r6["pnl"]["revenue"]))
    r15 = compute(a.copy(tax_mode="УСН 15%"))
    p = r15["pnl"]
    # в каждом году налог = max(15 % прибыли, 1 % выручки) нарастающим итогом
    for year in {d.year for d in r15["dates"]}:
        idx = [i for i, d in enumerate(r15["dates"]) if d.year == year]
        exp = max(0.15 * sum(p["pretax"][i] for i in idx), 0.01 * sum(p["revenue"][i] for i in idx))
        assert sum(p["tax"][i] for i in idx) == pytest.approx(exp)
    rn = compute(a.copy(tax_mode="НПД"))
    assert rn["cash"]["tax_paid"][1:] == pytest.approx(rn["pnl"]["tax"][:-1])  # НПД — через месяц
    # УСН платится в месяце после квартала (январь, апрель, июль, октябрь)
    paid_months = {r6["dates"][i].month for i, v in enumerate(r6["cash"]["tax_paid"]) if v}
    assert paid_months <= {1, 4, 7, 10}


def test_scenarios_order_and_drivers(demo):
    k = [compute(demo, scenario=sc)["kpi"] for sc in (1, 2, 3)]
    assert k[1]["net_profit"] < k[0]["net_profit"] < k[2]["net_profit"]
    u1, u2 = compute(demo, scenario=1)["unit"], compute(demo, scenario=2)["unit"]
    assert all(b > a for a, b in zip(u1["goods"], u2["goods"]))      # курс выше — товар дороже
    assert all(b < a for a, b in zip(u1["price"], u2["price"]))      # цена ниже
    assert demo.copy(active_scenario=SCENARIOS[2]).scenario_index == 3


def test_sensitivity_center_matches_base(demo):
    grid = sensitivity(demo, "price")
    i = demo.sens_fx.index(demo.fx_base)
    j = demo.sens_price.index(0.0)
    assert grid[i][j] == pytest.approx(compute(demo)["kpi"]["net_profit"])
    row = grid[i]
    assert row == sorted(row)                       # дороже продаём — больше прибыль
    col = [line[j] for line in grid]
    assert col == sorted(col, reverse=True)         # дороже юань — меньше прибыль


def test_demo_story(demo):
    """Демо показывает то, ради чего модель покупают: разрыв в базе, окупаемость в горизонте."""
    k = compute(demo)["kpi"]
    assert k["max_gap"] > 0 and k["net_profit"] > 0
    assert isinstance(k["payback"], int) and k["payback"] <= 18
    assert k["lost_units"] == 0
