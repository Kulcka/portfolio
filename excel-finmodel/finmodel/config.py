"""Допущения модели: структура, загрузка из JSON и проверка.

Один и тот же объект `Assumptions` питает и генератор книги Excel (build.py),
и зеркальный расчёт на Python (mirror.py) — так сверка через Excel сравнивает
две независимые реализации на одних и тех же входных данных.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

HORIZON = 18  # месяцев в модели

FREIGHT_METHODS = ["по весу", "по объёму", "по большему"]
TAX_MODES = ["УСН 6%", "УСН 15%", "НПД"]
SCENARIOS = ["Базовый", "Пессимистичный", "Оптимистичный"]

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
DEMO_CONFIG_PATH = EXAMPLES_DIR / "demo-assumptions.json"


@dataclass
class Sku:
    name: str
    price_cny: float        # закупочная цена, ¥/шт
    weight_kg: float        # вес брутто с упаковкой, кг/шт
    volume_m3: float        # объём с упаковкой, м³/шт
    batch: int              # партия (кратность заказа), шт
    price_rub: float        # цена продажи на маркетплейсе, ₽
    plateau: float          # продажи (выкупы) на плато, шт/мес
    duty_rate: float        # пошлина, доля от таможенной стоимости
    vat_rate: float         # НДС на ввоз, доля
    cert_rub: float         # сертификация / декларация, ₽ разово
    pack_rub: float         # упаковка и маркировка, ₽/шт
    mp_commission: float    # комиссия маркетплейса, доля от цены
    mp_logistics: float     # логистика маркетплейса до покупателя, ₽ за заказ
    returns: float          # возвраты, доля заказов
    start_stock: int = 0    # остаток на складе МП на старте, шт


@dataclass
class Scenario:
    fx: float      # изменение курса к базе, доля (+0.15 = курс на 15 % выше)
    price: float   # изменение цены продажи, доля
    sales: float   # изменение темпа продаж, доля
    ads: float     # изменение доли рекламы, п.п. в долях (0.03 = +3 п.п.)


@dataclass
class Assumptions:
    start_date: dt.date
    start_capital: float
    fx_base: float                 # ₽ за 1 ¥
    agent_fee: float               # комиссия агента в Китае, доля от товара
    bank_fee: float                # банк и конвертация, доля от (товар + агент)
    freight_method: str            # см. FREIGHT_METHODS
    freight_per_kg: float          # доставка Китай → РФ, ₽/кг
    freight_per_m3: float          # доставка Китай → РФ, ₽/м³
    customs_fee_per_batch: float   # таможенное оформление, ₽ за партию SKU
    inbound_per_kg: float          # доставка до склада МП, ₽/кг
    reverse_logistics: float       # обратная логистика МП, ₽ за возврат
    ads_rate: float                # реклама, доля выручки
    fixed_monthly: float           # постоянные расходы, ₽/мес
    startup_costs: float           # разовые стартовые расходы в 1-м месяце, ₽
    lead_time_months: int          # срок поставки от оплаты до склада МП, мес
    safety_months: int             # страховой запас, мес продаж
    payout_delay_days: float       # задержка выплат маркетплейса, дней
    days_in_month: float           # дней в месяце для пересчёта задержки
    tax_mode: str                  # см. TAX_MODES
    tax_usn6: float
    tax_usn15: float
    tax_usn_min: float             # минимальный налог УСН 15 %, доля доходов
    tax_npd: float
    npd_limit: float               # лимит дохода НПД, ₽/год
    usn_vat_threshold: float       # порог доходов УСН для НДС, ₽/год
    ramp: list[float]              # темп продаж по месяцам, доля от плато
    scenarios: dict[str, Scenario]
    active_scenario: str
    skus: list[Sku]
    sens_fx: list[float] = field(default_factory=list)      # ось курса, ₽/¥
    sens_price: list[float] = field(default_factory=list)   # ось цены, доля
    sens_sales: list[float] = field(default_factory=list)   # ось объёма, доля
    title: str = "Финансовая модель: импорт из Китая и продажа на маркетплейсах"
    demo_note: str = ""

    # ----------------------------------------------------------------- helpers
    @property
    def n_skus(self) -> int:
        return len(self.skus)

    @property
    def scenario_index(self) -> int:
        """Номер активного сценария, 1..3 (как MATCH в Excel)."""
        return SCENARIOS.index(self.active_scenario) + 1

    def copy(self, **changes) -> "Assumptions":
        new = copy.deepcopy(self)
        for k, v in changes.items():
            if not hasattr(new, k):
                raise AttributeError(k)
            setattr(new, k, v)
        new.validate()
        return new

    def validate(self) -> None:
        errors = []
        if len(self.ramp) != HORIZON:
            errors.append(f"ramp: нужно {HORIZON} значений, дано {len(self.ramp)}")
        if not 1 <= self.n_skus <= 12:
            errors.append("skus: от 1 до 12 товаров")
        if self.freight_method not in FREIGHT_METHODS:
            errors.append(f"freight_method: одно из {FREIGHT_METHODS}")
        if self.tax_mode not in TAX_MODES:
            errors.append(f"tax_mode: одно из {TAX_MODES}")
        if list(self.scenarios) != SCENARIOS:
            errors.append(f"scenarios: ровно {SCENARIOS} в этом порядке")
        if self.active_scenario not in SCENARIOS:
            errors.append(f"active_scenario: одно из {SCENARIOS}")
        if not 1 <= self.lead_time_months <= 6:
            errors.append("lead_time_months: 1..6")
        if not 0 <= self.safety_months <= 3:
            errors.append("safety_months: 0..3")
        if not 0 <= self.payout_delay_days <= 120:
            errors.append("payout_delay_days: 0..120")
        if self.days_in_month <= 0:
            errors.append("days_in_month > 0")
        for i, s in enumerate(self.skus, 1):
            if s.batch < 1:
                errors.append(f"SKU {i}: партия ≥ 1")
            if not 0 <= s.returns < 1:
                errors.append(f"SKU {i}: возвраты 0..<100 %")
            for fname in ("price_cny", "weight_kg", "volume_m3", "price_rub", "plateau",
                          "duty_rate", "vat_rate", "cert_rub", "pack_rub",
                          "mp_commission", "mp_logistics", "start_stock"):
                if getattr(s, fname) < 0:
                    errors.append(f"SKU {i}: {fname} < 0")
        for name, axis in (("sens_fx", self.sens_fx), ("sens_price", self.sens_price),
                           ("sens_sales", self.sens_sales)):
            if len(axis) != 7:
                errors.append(f"{name}: нужно 7 значений")
        if errors:
            raise ValueError("Ошибки в допущениях:\n  " + "\n  ".join(errors))

    # --------------------------------------------------------------- serialize
    def to_json(self) -> str:
        d = asdict(self)
        d["start_date"] = self.start_date.isoformat()
        return json.dumps(d, ensure_ascii=False, indent=2)


def from_dict(d: dict) -> Assumptions:
    d = dict(d)
    d.pop("_comment", None)
    d["start_date"] = dt.date.fromisoformat(d["start_date"])
    d["skus"] = [Sku(**s) for s in d["skus"]]
    d["scenarios"] = {k: Scenario(**v) for k, v in d["scenarios"].items()}
    a = Assumptions(**d)
    a.validate()
    return a


def load_config(path: str | Path) -> Assumptions:
    with open(path, encoding="utf-8") as f:
        return from_dict(json.load(f))


def demo_config() -> Assumptions:
    """Демо-допущения из examples/demo-assumptions.json (пример, не данные поставщика)."""
    return load_config(DEMO_CONFIG_PATH)
