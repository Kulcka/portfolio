import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from finmodel.build import build_workbook  # noqa: E402
from finmodel.config import Assumptions, Scenario, Sku, demo_config  # noqa: E402


@pytest.fixture(scope="session")
def demo() -> Assumptions:
    return demo_config()


@pytest.fixture(scope="session")
def built(demo):
    """Книга, собранная генератором (в памяти), и карта адресов."""
    return build_workbook(demo)


def simple_config(**over) -> Assumptions:
    """Один товар с круглыми числами — для проверки расчёта «вручную»."""
    sku = Sku(name="Тест", price_cny=100, weight_kg=1.0, volume_m3=0.01, batch=100,
              price_rub=5000, plateau=100, duty_rate=0.10, vat_rate=0.20, cert_rub=10000,
              pack_rub=50, mp_commission=0.20, mp_logistics=100, returns=0.0, start_stock=0)
    base = dict(
        start_date=__import__("datetime").date(2027, 1, 1), start_capital=1_000_000,
        fx_base=10.0, agent_fee=0.0, bank_fee=0.0, freight_method="по весу",
        freight_per_kg=200, freight_per_m3=20000, customs_fee_per_batch=5000,
        inbound_per_kg=50, reverse_logistics=0, ads_rate=0.10, fixed_monthly=0,
        startup_costs=0, lead_time_months=1, safety_months=0, payout_delay_days=0,
        days_in_month=30, tax_mode="УСН 6%", tax_usn6=0.06, tax_usn15=0.15,
        tax_usn_min=0.01, tax_npd=0.06, npd_limit=2_400_000,
        usn_vat_threshold=20_000_000, ramp=[1.0] * 18,
        scenarios={"Базовый": Scenario(0, 0, 0, 0),
                   "Пессимистичный": Scenario(0.1, -0.1, -0.2, 0.02),
                   "Оптимистичный": Scenario(-0.1, 0.1, 0.2, -0.02)},
        active_scenario="Базовый", skus=[sku],
        sens_fx=[8, 9, 10, 11, 12, 13, 14], sens_price=[-0.3, -0.2, -0.1, 0, 0.1, 0.2, 0.3],
        sens_sales=[-0.3, -0.2, -0.1, 0, 0.1, 0.2, 0.3])
    base.update(over)
    a = Assumptions(**base)
    a.validate()
    return a
