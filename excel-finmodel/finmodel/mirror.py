"""Зеркальная модель на Python — независимый расчёт тех же величин, что и книга.

Никаких формул Excel здесь нет: та же бизнес-логика написана обычными циклами.
Сверка (excel_check.py и тесты) пересчитывает книгу в настоящем Excel и
сравнивает каждую ячейку с этим расчётом.

Соглашения:
* месяцы нумеруются с 1 (как в строке «Месяц №» книги), списки — с 0;
* суммы в рублях, доли — в долях единицы (0.2 = 20 %);
* округление повторяет Excel: ROUND — половина от нуля после сведения
  числа к 15 значащим цифрам (как Excel хранит отображаемое значение).
"""
from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal

from .config import FREIGHT_METHODS, HORIZON, SCENARIOS, TAX_MODES, Assumptions


# --------------------------------------------------------------------------
# Excel-совместимые функции
# --------------------------------------------------------------------------
def _dec(x: float) -> Decimal:
    return Decimal(f"{x:.15g}")


def xl_round(x: float, digits: int = 0) -> float:
    """ROUND Excel: половина — от нуля (ROUND(2.5,0)=3, ROUND(-2.5,0)=-3)."""
    return float(_dec(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP))


def xl_roundup(x: float, digits: int = 0) -> float:
    """ROUNDUP Excel: от нуля."""
    return float(_dec(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_UP))


def edate(d: dt.date, months: int) -> dt.date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    # день месяца — не больше последнего дня целевого месяца
    for day in (d.day, 30, 29, 28):
        try:
            return dt.date(y, m, min(d.day, day))
        except ValueError:
            continue
    raise ValueError(d)


def _safe_div(a: float, b: float) -> float:
    """IFERROR(a/b, 0)."""
    return a / b if b != 0 else 0.0


# --------------------------------------------------------------------------
# Модель
# --------------------------------------------------------------------------
def compute(a: Assumptions, scenario: int | None = None, fx_override: float = 0.0,
            price_delta: float = 0.0, sales_delta: float = 0.0) -> dict:
    """Полный расчёт модели.

    scenario     — номер сценария 1..3 (None = активный из допущений);
    fx_override  — курс для анализа чувствительности (0 = не задан);
    price_delta, sales_delta — изменения цены/объёма для анализа чувствительности.
    """
    N = HORIZON
    K = a.n_skus
    L = a.lead_time_months
    SF = a.safety_months
    months = list(range(1, N + 1))

    # ---- активные значения (лист «Сценарии»)
    sc = scenario if scenario else a.scenario_index
    scen = a.scenarios[SCENARIOS[sc - 1]]
    if fx_override > 0:
        fx = fx_override
    else:
        fx = a.fx_base * (1 + scen.fx)
    price_mult = (1 + scen.price) * (1 + price_delta)
    sales_mult = (1 + scen.sales) * (1 + sales_delta)
    ads = max(0.0, a.ads_rate + scen.ads)
    tax_mode = TAX_MODES.index(a.tax_mode) + 1
    freight_mode = FREIGHT_METHODS.index(a.freight_method) + 1

    # ---- юнит-экономика (на 1 проданную единицу)
    u: dict[str, list] = {k: [] for k in (
        "price", "price_cny", "fx", "goods", "agent", "bank", "freight", "customs_value",
        "duty", "vat", "customs_fee", "pack", "inbound", "landed", "pay_order",
        "pay_arrival", "commission", "mp_logistics", "ads", "pretax", "tax", "full_cost",
        "profit", "margin", "markup", "roi", "plateau_eff", "rev_share", "fixed_alloc",
        "bep", "cert_payback")}
    total_plateau_rev = sum(s.price_rub * price_mult * s.plateau for s in a.skus)
    for s in a.skus:
        price = s.price_rub * price_mult
        goods = s.price_cny * fx
        agent = goods * a.agent_fee
        bank = (goods + agent) * a.bank_fee
        by_kg = s.weight_kg * a.freight_per_kg
        by_m3 = s.volume_m3 * a.freight_per_m3
        freight = {1: by_kg, 2: by_m3, 3: max(by_kg, by_m3)}[freight_mode]
        customs_value = goods + freight
        duty = customs_value * s.duty_rate
        vat = (customs_value + duty) * s.vat_rate
        customs_fee = _safe_div(a.customs_fee_per_batch, s.batch)
        pack = s.pack_rub
        inbound = s.weight_kg * a.inbound_per_kg
        landed = goods + agent + bank + freight + duty + vat + customs_fee + pack + inbound
        pay_order = goods + agent + bank
        pay_arrival = landed - pay_order
        commission = price * s.mp_commission
        mp_log = (s.mp_logistics + s.returns * a.reverse_logistics) / (1 - s.returns)
        ads_unit = price * ads
        pretax = price - landed - commission - mp_log - ads_unit
        if tax_mode == 1:
            tax = price * a.tax_usn6
        elif tax_mode == 2:
            tax = max(pretax * a.tax_usn15, price * a.tax_usn_min)
        else:
            tax = price * a.tax_npd
        full_cost = landed + commission + mp_log + ads_unit + tax
        profit = price - full_cost
        plateau_eff = s.plateau * sales_mult
        rev_share = _safe_div(price * s.plateau, total_plateau_rev)
        fixed_alloc = a.fixed_monthly * rev_share
        bep = fixed_alloc / profit if profit > 0 else "убыток на ед."
        cert_payback = s.cert_rub / profit if profit > 0 else "—"
        for key, val in (("price", price), ("price_cny", s.price_cny), ("fx", fx),
                         ("goods", goods), ("agent", agent), ("bank", bank),
                         ("freight", freight), ("customs_value", customs_value),
                         ("duty", duty), ("vat", vat), ("customs_fee", customs_fee),
                         ("pack", pack), ("inbound", inbound), ("landed", landed),
                         ("pay_order", pay_order), ("pay_arrival", pay_arrival),
                         ("commission", commission), ("mp_logistics", mp_log),
                         ("ads", ads_unit), ("pretax", pretax), ("tax", tax),
                         ("full_cost", full_cost), ("profit", profit),
                         ("margin", _safe_div(profit, price)),
                         ("markup", _safe_div(price - landed, landed)),
                         ("roi", _safe_div(profit, landed)),
                         ("plateau_eff", plateau_eff), ("rev_share", rev_share),
                         ("fixed_alloc", fixed_alloc), ("bep", bep),
                         ("cert_payback", cert_payback)):
            u[key].append(val)

    # ---- даты
    dates = [edate(a.start_date, t - 1) for t in months]
    years = [d.year for d in dates]

    # ---- закупки и склад
    fl = {k: [[0.0] * N for _ in range(K)] for k in (
        "demand", "arrivals", "sales", "lost", "stock", "transit", "need", "batches", "orders")}
    for k, s in enumerate(a.skus):
        demand = [xl_round(s.plateau * a.ramp[t - 1] * sales_mult, 0) for t in months]
        fl["demand"][k] = demand
        orders = [0.0] * N
        prev_stock = float(s.start_stock)
        for t in months:
            i = t - 1
            arrivals = sum(orders[j - 1] for j in range(1, t) if j == t - L)
            sales = min(demand[i], prev_stock + arrivals)
            stock = prev_stock + arrivals - sales
            transit = sum(orders[j - 1] for j in range(1, t) if j > t - L)
            need = sum(demand[j - 1] for j in months if t < j <= t + L + SF)
            need += max(0, t + L + SF - N) * demand[N - 1]
            gap = need - stock - transit
            batches = xl_roundup(gap / s.batch, 0) if gap > 0 else 0.0
            orders[i] = batches * s.batch
            for key, val in (("arrivals", arrivals), ("sales", sales),
                             ("lost", demand[i] - sales), ("stock", stock),
                             ("transit", transit), ("need", need),
                             ("batches", batches), ("orders", orders[i])):
                fl[key][k][i] = val
            prev_stock = stock

    def col_sum(rows):
        return [sum(r[i] for r in rows) for i in range(N)]

    flow_total = {k: col_sum(fl[k]) for k in ("demand", "arrivals", "sales", "lost", "transit",
                                              "stock", "orders")}
    flow_total["stock_value"] = [sum(fl["stock"][k][i] * u["landed"][k] for k in range(K))
                                 for i in range(N)]

    # ---- ОПиУ
    p_sku = {
        "revenue": [[fl["sales"][k][i] * u["price"][k] for i in range(N)] for k in range(K)],
        "cogs": [[fl["sales"][k][i] * u["landed"][k] for i in range(N)] for k in range(K)],
    }
    p_sku["commission"] = [[p_sku["revenue"][k][i] * a.skus[k].mp_commission
                            for i in range(N)] for k in range(K)]
    p_sku["mp_logistics"] = [[fl["sales"][k][i] * u["mp_logistics"][k] for i in range(N)]
                             for k in range(K)]
    p = {key: col_sum(rows) for key, rows in p_sku.items()}
    p["gross"] = [p["revenue"][i] - p["cogs"][i] for i in range(N)]
    p["gross_margin"] = [_safe_div(p["gross"][i], p["revenue"][i]) for i in range(N)]
    p["ads"] = [p["revenue"][i] * ads for i in range(N)]
    p["fixed"] = [a.fixed_monthly] * N
    p["startup"] = [a.startup_costs if t == 1 else 0.0 for t in months]

    # сертификация — в месяц первого заказа по SKU
    c_sku: dict[str, list] = {}
    c_sku["cert"] = []
    for k, s in enumerate(a.skus):
        row = []
        for i in range(N):
            first = fl["orders"][k][i] > 0 and sum(fl["orders"][k][:i]) == 0
            row.append(s.cert_rub if first else 0.0)
        c_sku["cert"].append(row)
    p["cert"] = col_sum(c_sku["cert"])
    p["pretax"] = [p["gross"][i] - p["commission"][i] - p["mp_logistics"][i] - p["ads"][i]
                   - p["fixed"][i] - p["startup"][i] - p["cert"][i] for i in range(N)]
    p["year"] = years
    p["ytd_revenue"], p["ytd_base"], p["ytd_tax"], p["tax"] = [], [], [], []
    for i in range(N):
        ytd_rev = sum(p["revenue"][j] for j in range(i + 1) if years[j] == years[i])
        ytd_base = sum(p["pretax"][j] for j in range(i + 1) if years[j] == years[i])
        if tax_mode == 1:
            ytd_tax = a.tax_usn6 * ytd_rev
        elif tax_mode == 2:
            ytd_tax = max(a.tax_usn15 * ytd_base, a.tax_usn_min * ytd_rev)
        else:
            ytd_tax = a.tax_npd * ytd_rev
        prev = p["ytd_tax"][i - 1] if i > 0 and years[i - 1] == years[i] else 0.0
        p["ytd_revenue"].append(ytd_rev)
        p["ytd_base"].append(ytd_base)
        p["ytd_tax"].append(ytd_tax)
        p["tax"].append(ytd_tax - prev)
    p["net"] = [p["pretax"][i] - p["tax"][i] for i in range(N)]
    p["net_cum"] = [sum(p["net"][: i + 1]) for i in range(N)]
    p["net_margin"] = [_safe_div(p["net"][i], p["revenue"][i]) for i in range(N)]

    # ---- денежный поток
    c: dict[str, list] = {}
    delay_months = a.payout_delay_days / a.days_in_month
    lag = int(delay_months)          # INT() для неотрицательных чисел
    frac = delay_months - lag
    c["lag"] = lag
    c["frac"] = frac
    c["payout_base"] = [p["revenue"][i] - p["commission"][i] - p["mp_logistics"][i]
                        for i in range(N)]

    def base_at(t):
        return c["payout_base"][t - 1] if 1 <= t <= N else 0.0

    c["payout"] = [(1 - frac) * base_at(t - lag) + frac * base_at(t - lag - 1) for t in months]

    def per_unit_rows(units_key, unit_key):
        return [[fl[units_key][k][i] * u[unit_key][k] for i in range(N)] for k in range(K)]

    c_sku["pay_order"] = per_unit_rows("orders", "pay_order")
    c_sku["freight"] = per_unit_rows("arrivals", "freight")
    c_sku["duty"] = per_unit_rows("arrivals", "duty")
    c_sku["vat"] = per_unit_rows("arrivals", "vat")
    c_sku["customs_fee"] = per_unit_rows("arrivals", "customs_fee")
    prep_unit = [u["pack"][k] + u["inbound"][k] for k in range(K)]
    c_sku["prep"] = [[fl["arrivals"][k][i] * prep_unit[k] for i in range(N)] for k in range(K)]
    for key in ("pay_order", "freight", "duty", "vat", "customs_fee", "prep", "cert"):
        c[key] = col_sum(c_sku[key])
    c["ads"] = p["ads"]
    c["fixed"] = p["fixed"]
    c["startup"] = p["startup"]
    c["tax_paid"] = []
    for t in months:
        d = dates[t - 1]
        if tax_mode == 3:
            paid = p["tax"][t - 2] if t >= 2 else 0.0
        elif d.month % 3 == 1:
            paid = sum(p["tax"][j - 1] for j in months if t - 3 <= j < t)
        else:
            paid = 0.0
        c["tax_paid"].append(paid)
    out_keys = ("pay_order", "freight", "duty", "vat", "customs_fee", "prep", "cert",
                "ads", "fixed", "startup", "tax_paid")
    c["outflow"] = [sum(c[k][i] for k in out_keys) for i in range(N)]
    c["net_cf"] = [c["payout"][i] - c["outflow"][i] for i in range(N)]
    c["opening"], c["closing"] = [], []
    cash = a.start_capital
    for i in range(N):
        c["opening"].append(cash)
        cash = cash + c["net_cf"][i]
        c["closing"].append(cash)
    c["gap"] = [max(0.0, -x) for x in c["closing"]]
    c["gap_flag"] = [x < 0 for x in c["closing"]]
    c["cum_cf"] = [sum(c["net_cf"][: i + 1]) for i in range(N)]
    c["min_future"] = [min(c["cum_cf"][i:]) for i in range(N)]
    c["paid_back"] = [x >= 0 for x in c["min_future"]]
    c["cash_pos"] = [max(x, 0.0) for x in c["closing"]]
    c["cash_neg"] = [min(x, 0.0) for x in c["closing"]]

    # ---- итоги на конец периода и контроль баланса
    end = {}
    end["cash"] = c["closing"][-1]
    end["stock_value"] = flow_total["stock_value"][-1]
    end["transit_value"] = (sum(fl["transit"][k][-1] * u["pay_order"][k] for k in range(K))
                            + c["pay_order"][-1])
    end["receivable"] = sum(c["payout_base"]) - sum(c["payout"])
    end["tax_payable"] = sum(p["tax"]) - sum(c["tax_paid"])
    end["equity"] = (end["cash"] + end["stock_value"] + end["transit_value"]
                     + end["receivable"] - end["tax_payable"])
    end["start_stock_value"] = sum(a.skus[k].start_stock * u["landed"][k] for k in range(K))
    end["equity_expected"] = a.start_capital + end["start_stock_value"] + sum(p["net"])
    end["balance_diff"] = end["equity"] - end["equity_expected"]
    end["balance_ok"] = "OK" if xl_round(end["balance_diff"], 2) == 0 else "Ошибка"

    # ---- ключевые показатели (дашборд)
    kpi = {}
    kpi["revenue"] = sum(p["revenue"])
    kpi["net_profit"] = sum(p["net"])
    kpi["net_margin"] = _safe_div(kpi["net_profit"], kpi["revenue"])
    kpi["gross_margin"] = _safe_div(sum(p["gross"]), kpi["revenue"])
    kpi["peak_investment"] = max(0.0, -min(c["cum_cf"]))
    kpi["max_gap"] = max(c["gap"])
    kpi["first_gap"] = (dates[c["gap_flag"].index(True)] if True in c["gap_flag"] else "нет")
    kpi["payback"] = (c["paid_back"].index(True) + 1 if True in c["paid_back"]
                      else f"более {N}")
    kpi["roi"] = _safe_div(kpi["net_profit"], kpi["peak_investment"])
    kpi["end_equity"] = end["equity"]
    kpi["end_cash"] = end["cash"]
    kpi["lost_units"] = sum(flow_total["lost"])
    kpi["sold_units"] = sum(flow_total["sales"])
    first_year = a.start_date.year
    kpi["annual_revenue"] = [sum(p["revenue"][i] for i in range(N) if years[i] == first_year + j)
                             for j in range(3)]
    kpi["max_annual_revenue"] = max(kpi["annual_revenue"])
    kpi["sku_sold"] = [sum(fl["sales"][k]) for k in range(K)]
    kpi["sku_revenue"] = [sum(p_sku["revenue"][k]) for k in range(K)]
    kpi["sku_lost"] = [sum(fl["lost"][k]) for k in range(K)]

    return {
        "active": {"scenario": sc, "fx": fx, "price_mult": price_mult,
                   "sales_mult": sales_mult, "ads": ads, "tax_mode": tax_mode,
                   "freight_mode": freight_mode},
        "dates": dates,
        "unit": u,
        "flow": fl,
        "flow_total": flow_total,
        "pnl": p,
        "pnl_sku": p_sku,
        "cash": c,
        "cash_sku": c_sku,
        "end": end,
        "kpi": kpi,
    }


def sensitivity(a: Assumptions, kind: str) -> list[list[float]]:
    """Таблицы листа «Чувствительность»: строки — курс, столбцы — цена или объём."""
    grid = []
    for fx in a.sens_fx:
        row = []
        cols = a.sens_price if kind == "price" else a.sens_sales
        for d in cols:
            if kind == "price":
                r = compute(a, fx_override=fx, price_delta=d)
                row.append(r["kpi"]["net_profit"])
            elif kind == "sales":
                r = compute(a, fx_override=fx, sales_delta=d)
                row.append(r["kpi"]["net_profit"])
            elif kind == "peak":
                r = compute(a, fx_override=fx, sales_delta=d)
                row.append(r["kpi"]["peak_investment"])
            else:
                raise ValueError(kind)
        grid.append(row)
    return grid


SCENARIO_TABLE_KEYS = ["net_profit", "revenue", "net_margin", "roi", "peak_investment",
                       "max_gap", "payback"]


def scenario_table(a: Assumptions) -> list[list]:
    """Таблица «Сравнение сценариев»: по строке на сценарий."""
    rows = []
    for sc in (1, 2, 3):
        k = compute(a, scenario=sc)["kpi"]
        rows.append([k[key] for key in SCENARIO_TABLE_KEYS])
    return rows
