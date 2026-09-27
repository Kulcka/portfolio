"""Генератор книги Excel с живыми формулами (openpyxl).

Запуск:
    python -m finmodel.build                       # демо-допущения → examples/
    python -m finmodel.build --config my.json --out my-model.xlsx

Все расчётные ячейки — формулы Excel; числа вписаны только в ячейки ввода
(жёлтые, не защищены) и в три служебные ячейки анализа (0). Вместе с книгой
генератор возвращает `Layout` — карту адресов, по которой сверка через Excel
находит нужные ячейки.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference, Series
from openpyxl.chart.layout import Layout as ChartLayout
from openpyxl.chart.layout import ManualLayout
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule, FormulaRule
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter as CL
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.formula import DataTableFormula
from openpyxl.worksheet.properties import PageSetupProperties

from . import styles as S
from .config import FREIGHT_METHODS, HORIZON, SCENARIOS, TAX_MODES, Assumptions, demo_config

# ------------------------------------------------------------------ имена листов
SH_INFO = "Инструкция"
SH_DASH = "Дашборд"
SH_IN = "Допущения"
SH_SC = "Сценарии"
SH_UNIT = "Юнит-экономика"
SH_FLOW = "Закупки и склад"
SH_CASH = "Денежный поток"
SH_PNL = "ОПиУ"
SH_SENS = "Чувствительность"
SHEET_ORDER = [SH_INFO, SH_DASH, SH_IN, SH_SC, SH_UNIT, SH_FLOW, SH_CASH, SH_PNL, SH_SENS]

# ------------------------------------------------------------ сетка помесячных листов
COL_LABEL, COL_UNIT, COL_PARAM, COL_TOTAL, COL_START = 1, 2, 3, 4, 5
ROW_IDX, ROW_DATE = 4, 5          # строки «Месяц №» и дат на помесячных листах
FIRST_DATA_ROW = 7


def mcol(t: int) -> int:
    """Столбец месяца t (1..HORIZON): F=6 ... W=23."""
    return COL_START + t


FIRST_M = CL(mcol(1))            # F
LAST_M = CL(mcol(HORIZON))       # W
START_L = CL(COL_START)          # E


class AttrDict(dict):
    """dict с доступом P.revenue — для вложенных f-строк Python 3.11."""
    __getattr__ = dict.__getitem__


def ref(sheet: str, addr: str) -> str:
    return f"'{sheet}'!{addr}"


def absr(col: int, row: int) -> str:
    return f"${CL(col)}${row}"


@dataclass
class Layout:
    """Карта адресов книги (для сверки и тестов)."""
    n_skus: int = 0
    names: dict = field(default_factory=AttrDict)          # имя → (лист, адрес)
    inputs: set = field(default_factory=set)           # (лист, адрес) ячеек ввода
    service: set = field(default_factory=set)          # (лист, адрес) служебных констант
    in_rows: dict = field(default_factory=AttrDict)        # Допущения: ключ SKU → строка
    in_sku_col0: int = 3
    ramp_rows: list = field(default_factory=list)
    unit_rows: dict = field(default_factory=AttrDict)      # Юнит-экономика: ключ → строка
    unit_col0: int = 3
    flow_rows: dict = field(default_factory=AttrDict)      # ключ → [строка по SKU]
    flow_total_rows: dict = field(default_factory=AttrDict)
    pnl_rows: dict = field(default_factory=AttrDict)
    pnl_sku_rows: dict = field(default_factory=AttrDict)
    cash_rows: dict = field(default_factory=AttrDict)
    cash_sku_rows: dict = field(default_factory=AttrDict)
    cash_cells: dict = field(default_factory=AttrDict)     # lag/frac → адрес
    end_cells: dict = field(default_factory=AttrDict)      # итоги на конец → адрес (лист ДП)
    annual_rows: list = field(default_factory=list)    # ОПиУ: строки выручки по годам
    kpi_names: dict = field(default_factory=AttrDict)      # ключ зеркала → имя
    sens: dict = field(default_factory=AttrDict)           # вид → (строка, столбец) угла данных
    scen_table: tuple = (0, 0)                         # (строка, столбец) угла данных
    dash_sku_row0: int = 0
    data_table_ranges: list = field(default_factory=list)  # (лист, "D11:J17")
    charts: int = 0


class Builder:
    def __init__(self, a: Assumptions):
        a.validate()
        self.a = a
        self.K = a.n_skus
        self.wb = Workbook()
        self.L = Layout(n_skus=self.K)
        self.ws = {}
        first = self.wb.active
        first.title = SHEET_ORDER[0]
        self.ws[SHEET_ORDER[0]] = first
        for name in SHEET_ORDER[1:]:
            self.ws[name] = self.wb.create_sheet(name)
        for ws in self.ws.values():
            ws.sheet_view.showGridLines = False
            ws.sheet_view.zoomScale = 90

    # ================================================================ helpers
    def put(self, sheet, row, col, value, *, font=S.FONT, fill=None, fmt=None,
            align=None, border=None):
        c = self.ws[sheet].cell(row=row, column=col)
        c.value = value
        c.font = font
        if fill is not None:
            c.fill = fill
        if fmt is not None:
            c.number_format = fmt
        if align is not None:
            c.alignment = align
        if border is not None:
            c.border = border
        c.protection = S.LOCKED
        return c

    def put_input(self, sheet, row, col, value, fmt=None, bold=False):
        c = self.put(sheet, row, col, value, font=S.FONT_INPUT_BOLD if bold else S.FONT_INPUT,
                     fill=S.FILL_INPUT, fmt=fmt, border=S.BORDER)
        c.protection = S.UNLOCKED
        self.L.inputs.add((sheet, c.coordinate))
        return c

    def put_service(self, sheet, row, col, value, fmt=None):
        c = self.put(sheet, row, col, value, font=S.FONT_SERVICE, fill=S.FILL_SERVICE,
                     fmt=fmt, border=S.BORDER)
        self.L.service.add((sheet, c.coordinate))
        return c

    def name(self, nm: str, sheet: str, addr: str):
        """Именованный диапазон; addr — в абсолютной форме ($C$5 или $C$5:$C$9)."""
        self.wb.defined_names[nm] = DefinedName(nm, attr_text=f"'{sheet}'!{addr}")
        self.L.names[nm] = (sheet, addr)

    def title(self, sheet, text, note=None, demo=True):
        self.put(sheet, 1, 1, text, font=S.FONT_TITLE)
        if note:
            self.put(sheet, 2, 1, note, font=S.FONT_NOTE)
        if demo and self.a.demo_note:
            self.put(sheet, 3 if note else 2, 1, self.a.demo_note, font=S.FONT_DEMO)

    def section(self, sheet, row, text, c1=1, c2=None):
        c2 = c2 or (COL_START + HORIZON)
        for col in range(c1, c2 + 1):
            self.put(sheet, row, col, None, fill=S.FILL_SECTION)
        self.put(sheet, row, c1, text, font=S.FONT_BOLD, fill=S.FILL_SECTION)

    def header_row(self, sheet, row, values, c1=1):
        for i, v in enumerate(values):
            self.put(sheet, row, c1 + i, v, font=S.FONT_HEADER, fill=S.FILL_HEADER,
                     align=S.AL_CENTER, border=S.BORDER)

    def month_grid_header(self, sheet):
        """Строки «Месяц №» и дат, заголовки столбцов A–E. Все значения — формулы."""
        self.put(sheet, ROW_IDX, COL_LABEL, "Месяц №", font=S.FONT_NOTE)
        for t in range(1, HORIZON + 1):
            col = mcol(t)
            self.put(sheet, ROW_IDX, col, f"=COLUMN()-COLUMN(${START_L}${ROW_IDX})",
                     font=S.FONT_NOTE, fmt=S.F_INT, align=S.AL_CENTER)
            self.put(sheet, ROW_DATE, col, f"=EDATE(Дата_начала,{CL(col)}${ROW_IDX}-1)",
                     font=S.FONT_HEADER, fill=S.FILL_HEADER, fmt=S.F_DATE, align=S.AL_CENTER)
        heads = ["Показатель", "Ед.", "Параметр", "Итого", "Старт"]
        for i, h in enumerate(heads):
            self.put(sheet, ROW_DATE, 1 + i, h, font=S.FONT_HEADER, fill=S.FILL_HEADER,
                     align=S.AL_CENTER)
        ws = self.ws[sheet]
        ws.column_dimensions["A"].width = 50
        ws.column_dimensions["B"].width = 9
        ws.column_dimensions["C"].width = 12
        ws.column_dimensions["D"].width = 14
        ws.column_dimensions["E"].width = 10
        for t in range(1, HORIZON + 1):
            ws.column_dimensions[CL(mcol(t))].width = 11.5
        ws.freeze_panes = f"{FIRST_M}{ROW_DATE + 1}"
        ws.print_title_rows = f"{ROW_IDX}:{ROW_DATE}"
        ws.print_title_cols = "A:B"

    def month_row(self, sheet, row, label, unit, formula_fn, *, fmt=S.F_RUB, bold=False,
                  indent=False, total="sum", param=None, param_fmt=None, start=None,
                  start_fmt=None, service=False, fill=None):
        """Строка помесячного листа. formula_fn(t, col_letter, prev_letter) → формула."""
        font = S.FONT_BOLD if bold else (S.FONT_SERVICE if service else S.FONT)
        fill = fill if fill is not None else (S.FILL_TOTAL if bold else None)
        self.put(sheet, row, COL_LABEL, label, font=font, fill=fill,
                 align=S.AL_INDENT if indent else S.AL_LEFT)
        self.put(sheet, row, COL_UNIT, unit, font=S.FONT_NOTE, fill=fill, align=S.AL_CENTER)
        self.put(sheet, row, COL_PARAM, param, font=S.FONT_NOTE, fill=fill,
                 fmt=param_fmt or fmt)
        if total == "sum":
            tot = f"=SUM({FIRST_M}{row}:{LAST_M}{row})"
        else:
            tot = None
        self.put(sheet, row, COL_TOTAL, tot, font=S.FONT_BOLD, fill=fill or S.FILL_TOTAL, fmt=fmt)
        self.put(sheet, row, COL_START, start, font=font, fill=fill, fmt=start_fmt or fmt)
        for t in range(1, HORIZON + 1):
            col = mcol(t)
            self.put(sheet, row, col, formula_fn(t, CL(col), CL(col - 1)), font=font,
                     fill=fill, fmt=fmt)

    # ================================================================ Допущения
    def build_inputs(self):
        sh = SH_IN
        a = self.a
        ws = self.ws[sh]
        self.title(sh, "Допущения — ввод данных",
                   "Жёлтые ячейки — ввод (можно менять). Остальные ячейки считаются "
                   "формулами и защищены. Ставки пошлин, НДС, налогов и тарифы "
                   "маркетплейса — параметры: уточните их под свой товар.")
        ws.column_dimensions["A"].width = 58
        ws.column_dimensions["B"].width = 12
        for k in range(max(self.K, 1)):
            ws.column_dimensions[CL(3 + k)].width = 17
        comment_col = 3 + max(self.K, 1)
        ws.column_dimensions[CL(comment_col)].width = 70

        r = 5
        self.section(sh, r, "1. Общие параметры", 1, comment_col)
        r += 1
        self.header_row(sh, r, ["Параметр", "Ед.", "Значение"])
        self.put(sh, r, 4, "Комментарий", font=S.FONT_HEADER, fill=S.FILL_HEADER)
        r += 1
        general = [
            ("Дата_начала", "Первый месяц модели (месяц первой оплаты поставщику)", "дата",
             a.start_date, S.F_DATE_FULL, "Горизонт модели — 18 месяцев с этой даты"),
            ("Капитал_старт", "Стартовый капитал (свои деньги на счёте)", "₽",
             a.start_capital, S.F_RUB, "Если остаток денег уходит в минус — это кассовый разрыв"),
            ("Курс_база", "Курс юаня, базовый", "₽ за ¥", a.fx_base, S.F_FX,
             "Сценарии меняют курс относительно этого значения"),
            ("Комиссия_агента", "Комиссия агента / байера в Китае", "% товара", a.agent_fee,
             S.F_PCT_IN, "Поиск поставщика, выкуп, контроль качества — по договору с агентом"),
            ("Банк_комиссия", "Банк и конвертация при оплате поставщику", "%", a.bank_fee,
             S.F_PCT_IN, "От суммы «товар + агент»; платёжный агент или банк"),
            ("Доставка_способ", "Как считать доставку из Китая", "список", a.freight_method,
             None, "по весу / по объёму / по большему из двух — как считает ваш перевозчик"),
            ("Доставка_кг", "Доставка Китай → склад в РФ, тариф по весу", "₽/кг",
             a.freight_per_kg, S.F_RUB, "Уточните у карго / экспедитора"),
            ("Доставка_м3", "Доставка Китай → склад в РФ, тариф по объёму", "₽/м³",
             a.freight_per_m3, S.F_RUB, "Уточните у карго / экспедитора"),
            ("Тамож_оформление", "Таможенное оформление (брокер, сборы) на одну партию товара",
             "₽/партию", a.customs_fee_per_batch, S.F_RUB,
             "Уточните у таможенного брокера. Если карго «под ключ» — 0, а пошлину и НДС "
             "на ввоз включите в тариф доставки"),
            ("Доставка_до_МП", "Доставка со склада в РФ до склада маркетплейса", "₽/кг",
             a.inbound_per_kg, S.F_RUB, "Фулфилмент / транспортная компания"),
            ("Возврат_логистика", "Обратная логистика маркетплейса за один возврат", "₽/шт",
             a.reverse_logistics, S.F_RUB, "Уточните по тарифам своей площадки"),
            ("ДРР_база", "Реклама на маркетплейсе (доля рекламных расходов)", "% выручки",
             a.ads_rate, S.F_PCT_IN, "Сценарии добавляют или убавляют п.п."),
            ("Пост_расходы", "Постоянные расходы (бухгалтерия, сервисы, персонал)", "₽/мес",
             a.fixed_monthly, S.F_RUB, "Каждый месяц с первого"),
            ("Старт_расходы", "Разовые стартовые расходы (образцы, фото, карточки)", "₽",
             a.startup_costs, S.F_RUB, "Списываются в первом месяце"),
            ("Срок_поставки", "Срок поставки: от оплаты поставщику до склада маркетплейса",
             "мес", a.lead_time_months, S.F_INT,
             "Целое число 1–6. Товар поступает в продажу через столько месяцев после заказа"),
            ("Страх_запас", "Страховой запас", "мес продаж", a.safety_months, S.F_INT,
             "Целое число 0–3. Сколько месяцев продаж держать сверх срока поставки"),
            ("Задержка_выплат_дн", "Задержка выплат маркетплейса после продажи", "дней",
             a.payout_delay_days, S.F_INT, "0–120 дней. Дробная часть месяца делится между месяцами"),
            ("Дней_в_месяце", "Дней в месяце (для пересчёта задержки выплат)", "дней",
             a.days_in_month, S.F_INT, "Обычно 30"),
        ]
        dv_pct = DataValidation(type="decimal", operator="between", formula1="0", formula2="1",
                                showErrorMessage=True, errorTitle="Проверка ввода",
                                error="Введите долю от 0 до 100 %")
        dv_pos = DataValidation(type="decimal", operator="greaterThanOrEqual", formula1="0",
                                showErrorMessage=True, errorTitle="Проверка ввода",
                                error="Значение не может быть отрицательным")
        dv_lead = DataValidation(type="whole", operator="between", formula1="1", formula2="6",
                                 showErrorMessage=True, error="Целое число от 1 до 6")
        dv_safe = DataValidation(type="whole", operator="between", formula1="0", formula2="3",
                                 showErrorMessage=True, error="Целое число от 0 до 3")
        dv_delay = DataValidation(type="decimal", operator="between", formula1="0",
                                  formula2="120", showErrorMessage=True, error="От 0 до 120 дней")
        dv_days = DataValidation(type="decimal", operator="between", formula1="28",
                                 formula2="31", showErrorMessage=True, error="От 28 до 31")
        dv_freight = DataValidation(type="list", formula1="Доставка_список", allow_blank=False,
                                    showErrorMessage=True, error="Выберите значение из списка")
        dv_tax = DataValidation(type="list", formula1="Налог_список", allow_blank=False,
                                showErrorMessage=True, error="Выберите режим из списка")
        dv_date = DataValidation(type="date", operator="greaterThan", formula1="36526",
                                 showErrorMessage=True, error="Введите дату")
        dv_batch = DataValidation(type="whole", operator="greaterThanOrEqual", formula1="1",
                                  showErrorMessage=True, error="Целое число не меньше 1")
        dv_ret = DataValidation(type="decimal", operator="between", formula1="0",
                                formula2="0.95", showErrorMessage=True, error="От 0 до 95 %")
        for dv in (dv_pct, dv_pos, dv_lead, dv_safe, dv_delay, dv_days, dv_freight, dv_tax,
                   dv_date, dv_batch, dv_ret):
            ws.add_data_validation(dv)
        dv_by_name = {"Дата_начала": dv_date, "Комиссия_агента": dv_pct, "Банк_комиссия": dv_pct,
                      "ДРР_база": dv_pct, "Доставка_способ": dv_freight,
                      "Срок_поставки": dv_lead, "Страх_запас": dv_safe,
                      "Задержка_выплат_дн": dv_delay, "Дней_в_месяце": dv_days}
        for nm, label, unit, val, fmt, comment in general:
            self.put(sh, r, 1, label)
            self.put(sh, r, 2, unit, font=S.FONT_NOTE, align=S.AL_CENTER)
            c = self.put_input(sh, r, 3, val, fmt=fmt)
            self.put(sh, r, 4, comment, font=S.FONT_NOTE)
            self.name(nm, sh, absr(3, r))
            dv_by_name.get(nm, dv_pos).add(c)
            r += 1

        # ---- налоги
        r += 1
        self.section(sh, r, "2. Налоги (ставки — параметры: проверьте свой режим и регион)",
                     1, comment_col)
        r += 1
        self.put(sh, r, 1, "Налоговый режим")
        self.put(sh, r, 2, "список", font=S.FONT_NOTE, align=S.AL_CENTER)
        c = self.put_input(sh, r, 3, a.tax_mode, bold=True)
        dv_tax.add(c)
        self.name("Налог_режим", sh, absr(3, r))
        self.put(sh, r, 4, "УСН 6 % — с доходов; УСН 15 % — с доходов минус расходы; НПД — "
                           "для сравнения", font=S.FONT_NOTE)
        r += 1
        self.header_row(sh, r, ["Режим", "Ед.", "Ставка"])
        self.put(sh, r, 4, "Комментарий", font=S.FONT_HEADER, fill=S.FILL_HEADER)
        r += 1
        tax_rows = [
            ("Ставка_УСН6", TAX_MODES[0], a.tax_usn6,
             "Налог с выручки. Базовая ставка; регион может установить ниже — уточните"),
            ("Ставка_УСН15", TAX_MODES[1], a.tax_usn15,
             "Налог с разницы «доходы − расходы», считается нарастающим итогом с начала года"),
            ("Ставка_НПД", TAX_MODES[2], a.tax_npd,
             "Внимание: НПД не применяется при перепродаже товаров (422-ФЗ, ст. 4) — "
             "режим оставлен для сравнения"),
        ]
        first_tax_row = r
        for nm, mode, val, comment in tax_rows:
            self.put(sh, r, 1, mode, font=S.FONT_BOLD)
            self.put(sh, r, 2, "%", font=S.FONT_NOTE, align=S.AL_CENTER)
            c = self.put_input(sh, r, 3, val, fmt=S.F_PCT_IN)
            dv_pct.add(c)
            self.put(sh, r, 4, comment, font=S.FONT_NOTE)
            self.name(nm, sh, absr(3, r))
            r += 1
        self.name("Налог_список", sh, f"{absr(1, first_tax_row)}:{absr(1, r - 1)}")
        for nm, label, unit, val, fmt, comment in [
            ("Мин_налог_УСН15", "Минимальный налог при УСН 15 %", "% доходов", a.tax_usn_min,
             S.F_PCT_IN, "В модели — нарастающим итогом с начала года (консервативно)"),
            ("Лимит_НПД", "Лимит дохода для НПД", "₽/год", a.npd_limit, S.F_RUB,
             "Параметр для предупреждения на дашборде — проверьте актуальный лимит"),
            ("Порог_НДС_УСН", "Порог доходов, выше которого УСН платит НДС", "₽/год",
             a.usn_vat_threshold, S.F_RUB,
             "Пример — проверьте порог на свой год. НДС на УСН модель не считает, "
             "только предупреждает о превышении"),
        ]:
            self.put(sh, r, 1, label)
            self.put(sh, r, 2, unit, font=S.FONT_NOTE, align=S.AL_CENTER)
            c = self.put_input(sh, r, 3, val, fmt=fmt)
            (dv_pct if fmt == S.F_PCT_IN else dv_pos).add(c)
            self.put(sh, r, 4, comment, font=S.FONT_NOTE)
            self.name(nm, sh, absr(3, r))
            r += 1

        # ---- товары
        r += 1
        self.section(sh, r, "3. Товары (SKU): одна колонка — один товар", 1, comment_col)
        r += 1
        self.header_row(sh, r, ["Параметр", "Ед."] + [f"Товар {k + 1}" for k in range(self.K)])
        self.put(sh, r, comment_col, "Комментарий", font=S.FONT_HEADER, fill=S.FILL_HEADER)
        r += 1
        sku_fields = [
            ("name", "Наименование", "", None, "Короткое название — попадёт в отчёты и графики"),
            ("price_cny", "Закупочная цена у поставщика", "¥/шт", S.F_CNY,
             "Цена фабрики (EXW/FOB) за единицу"),
            ("weight_kg", "Вес брутто с упаковкой", "кг/шт", S.F_KG, "Для тарифа доставки по весу"),
            ("volume_m3", "Объём с упаковкой", "м³/шт", S.F_M3,
             "Длина × ширина × высота коробки, м"),
            ("batch", "Партия закупки (кратность заказа)", "шт", S.F_QTY,
             "Минимальный заказ у поставщика; модель заказывает кратно партии"),
            ("price_rub", "Цена продажи на маркетплейсе", "₽", S.F_RUB,
             "Цена, которую платит покупатель (после скидок)"),
            ("plateau", "Продажи на плато (выкупы)", "шт/мес", S.F_QTY,
             "Сколько штук в месяц покупатели выкупают при 100 % темпа"),
            ("start_stock", "Остаток на складе МП на старте", "шт", S.F_QTY,
             "Если товар уже есть на складе; обычно 0"),
            ("duty_rate", "Ставка пошлины", "% тамож. стоим.", S.F_PCT_IN,
             "Уточните по коду ТН ВЭД своего товара"),
            ("vat_rate", "Ставка НДС на ввоз", "%", S.F_PCT_IN,
             "Уточните ставку для своего товара на дату ввоза"),
            ("cert_rub", "Сертификация / декларация соответствия", "₽ разово", S.F_RUB,
             "Платится один раз, в месяц первого заказа товара"),
            ("pack_rub", "Упаковка и маркировка", "₽/шт", S.F_RUB,
             "Пакет, этикетка, коды маркировки, если нужны"),
            ("mp_commission", "Комиссия маркетплейса", "% цены", S.F_PCT_IN,
             "Уточните для своей категории и площадки"),
            ("mp_logistics", "Логистика маркетплейса до покупателя", "₽/заказ", S.F_RUB,
             "Доставка + хранение на заказ; уточните по тарифам площадки"),
            ("returns", "Доля возвратов", "% заказов", S.F_PCT_IN,
             "Возвращённый товар возвращается в продажу; платится логистика туда и обратно"),
        ]
        for key, label, unit, fmt, comment in sku_fields:
            self.put(sh, r, 1, label, font=S.FONT_BOLD if key == "name" else S.FONT)
            self.put(sh, r, 2, unit, font=S.FONT_NOTE, align=S.AL_CENTER)
            for k, sku in enumerate(a.skus):
                c = self.put_input(sh, r, 3 + k, getattr(sku, key), fmt=fmt, bold=key == "name")
                if key == "name":
                    c.alignment = S.AL_CENTER
                elif key == "batch":
                    dv_batch.add(c)
                elif key == "returns":
                    dv_ret.add(c)
                elif fmt == S.F_PCT_IN:
                    dv_pct.add(c)
                else:
                    dv_pos.add(c)
            self.put(sh, r, comment_col, comment, font=S.FONT_NOTE)
            self.L.in_rows[key] = r
            r += 1

        # ---- темп продаж
        r += 1
        self.section(sh, r, "4. Темп продаж по месяцам, % от плато (сезонность и выход на плато)",
                     1, comment_col)
        r += 1
        self.header_row(sh, r, ["Месяц", "Месяц №", "% от плато"])
        self.put(sh, r, 4, "Первые месяцы товар едет из Китая — ставьте 0 %, если нет "
                           "начального остатка", font=S.FONT_NOTE)
        hdr = r
        r += 1
        first_ramp = r
        for t in range(1, HORIZON + 1):
            self.put(sh, r, 2, f"=ROW()-ROW($B${hdr})", fmt=S.F_INT, align=S.AL_CENTER)
            self.put(sh, r, 1, f"=EDATE(Дата_начала,B{r}-1)", fmt=S.F_DATE_MONTH, align=S.AL_LEFT)
            c = self.put_input(sh, r, 3, a.ramp[t - 1], fmt="0%")
            dv_pos.add(c)
            self.L.ramp_rows.append(r)
            r += 1
        self.name("Темп_продаж", sh, f"{absr(3, first_ramp)}:{absr(3, r - 1)}")

        # ---- справочник
        r += 1
        self.section(sh, r, "5. Справочник (не менять)", 1, comment_col)
        r += 1
        first = r
        for m in FREIGHT_METHODS:
            self.put(sh, r, 1, m, font=S.FONT_SERVICE)
            r += 1
        self.name("Доставка_список", sh, f"{absr(1, first)}:{absr(1, r - 1)}")
        ws.freeze_panes = "C5"

    def in_sku(self, key: str, k: int, absolute=True) -> str:
        """Ссылка на параметр SKU k на листе «Допущения»."""
        col = CL(self.L.in_sku_col0 + k)
        row = self.L.in_rows[key]
        return ref(SH_IN, f"${col}${row}" if absolute else f"{col}{row}")

    # ================================================================ Сценарии
    def build_scenarios(self):
        sh = SH_SC
        a = self.a
        ws = self.ws[sh]
        self.title(sh, "Сценарии",
                   "Выберите сценарий в жёлтой ячейке — пересчитается вся книга. Базовый = "
                   "значения листа «Допущения»; остальные сценарии — отклонения от них.")
        for col, w in zip("ABCDEFGHI", (44, 14, 16, 16, 16, 16, 18, 18, 16)):
            ws.column_dimensions[col].width = w
        r = 5
        self.put(sh, r, 1, "Активный сценарий", font=S.FONT_SUBTITLE)
        sel = self.put_input(sh, r, 3, a.active_scenario, bold=True)
        ws.merge_cells(start_row=r, start_column=3, end_row=r, end_column=4)
        self.name("Сценарий_выбор", sh, absr(3, r))
        r += 2
        self.section(sh, r, "Отклонения от базовых допущений", 1, 6)
        r += 1
        hdr = r
        self.header_row(sh, r, ["Параметр", "Ед."] + SCENARIOS + ["Действует сейчас"])
        dv = DataValidation(type="list", formula1=f"{absr(3, hdr)}:{absr(5, hdr)}",
                            allow_blank=False, showErrorMessage=True,
                            error="Выберите сценарий из списка")
        ws.add_data_validation(dv)
        dv.add(sel)
        r += 1
        rows = [("fx", "Курс юаня", "% к базе"), ("price", "Цена продажи", "% к базе"),
                ("sales", "Темп продаж", "% к базе"), ("ads", "Реклама (ДРР)", "п.п.")]
        delta_row = {}
        for key, label, unit in rows:
            self.put(sh, r, 1, label)
            self.put(sh, r, 2, unit, font=S.FONT_NOTE, align=S.AL_CENTER)
            for i, sc in enumerate(SCENARIOS):
                self.put_input(sh, r, 3 + i, getattr(a.scenarios[sc], key), fmt=S.F_PCT_DELTA)
            self.put(sh, r, 6, f"=INDEX($C{r}:$E{r},Сценарий_номер)", font=S.FONT_BOLD,
                     fmt=S.F_PCT_DELTA, fill=S.FILL_TOTAL, border=S.BORDER)
            delta_row[key] = r
            r += 1
        r += 1
        self.section(sh, r, "Действующие значения (их использует вся модель)", 1, 6)
        r += 1
        active = [
            ("Курс", "Курс юаня", "₽ за ¥",
             f"=IF(Курс_анализ>0,Курс_анализ,Курс_база*(1+$F${delta_row['fx']}))", S.F_FX),
            ("Множитель_цены", "Множитель цены продажи", "×",
             f"=(1+$F${delta_row['price']})*(1+Цена_анализ)", "0.000"),
            ("Множитель_продаж", "Множитель темпа продаж", "×",
             f"=(1+$F${delta_row['sales']})*(1+Объём_анализ)", "0.000"),
            ("ДРР", "Реклама, доля выручки", "%", f"=MAX(0,ДРР_база+$F${delta_row['ads']})",
             S.F_PCT),
            ("Налог_номер", "Налоговый режим (номер в списке)", "№",
             "=MATCH(Налог_режим,Налог_список,0)", S.F_INT),
            ("Доставка_номер", "Способ расчёта доставки (номер в списке)", "№",
             "=MATCH(Доставка_способ,Доставка_список,0)", S.F_INT),
        ]
        for nm, label, unit, formula, fmt in active:
            self.put(sh, r, 1, label)
            self.put(sh, r, 2, unit, font=S.FONT_NOTE, align=S.AL_CENTER)
            self.put(sh, r, 3, formula, font=S.FONT_BOLD, fmt=fmt, fill=S.FILL_TOTAL,
                     border=S.BORDER)
            self.name(nm, sh, absr(3, r))
            r += 1
        # номер сценария: служебная ячейка для таблицы сравнения
        override_row = r + 1
        self.put(sh, r, 1, "Номер активного сценария")
        self.put(sh, r, 2, "№", font=S.FONT_NOTE, align=S.AL_CENTER)
        self.put(sh, r, 3, f"=IF(Сценарий_ручной>0,Сценарий_ручной,"
                           f"MATCH(Сценарий_выбор,{absr(3, hdr)}:{absr(5, hdr)},0))",
                 font=S.FONT_BOLD, fmt=S.F_INT, fill=S.FILL_TOTAL, border=S.BORDER)
        self.name("Сценарий_номер", sh, absr(3, r))
        r += 1
        self.put(sh, r, 1, "Служебная: номер сценария для таблицы сравнения (держать 0)",
                 font=S.FONT_SERVICE)
        self.put_service(sh, r, 3, 0, fmt=S.F_INT)
        self.name("Сценарий_ручной", sh, absr(3, r))
        assert r == override_row

        # ---- сравнение сценариев (таблица данных Excel)
        r += 2
        self.section(sh, r, "Сравнение сценариев — все три сразу (таблица данных Excel, "
                            "считается автоматически)", 1, 9)
        r += 1
        cols = [("ЧП_итого", "Чистая прибыль за 18 мес, ₽", S.F_RUB_KPI),
                ("Выручка_итого", "Выручка за 18 мес, ₽", S.F_RUB_KPI),
                ("Рентабельность", "Рентабельность продаж", S.F_PCT),
                ("ROI_период", "ROI на вложенные деньги", "0%;[Red]-0%"),
                ("Пик_вложений", "Потребность в деньгах (пик), ₽", S.F_RUB_KPI),
                ("Макс_разрыв", "Макс. кассовый разрыв, ₽", S.F_RUB_KPI),
                ("Окупаемость_мес", "Окупаемость, мес", '0" мес"')]
        self.header_row(sh, r, ["Сценарий", "№"] + [c[1] for c in cols])
        ws.row_dimensions[r].height = 42
        r += 1
        top = r
        self.put(sh, top, 1, "Текущий расчёт (активный сценарий)", font=S.FONT_NOTE)
        self.put(sh, top, 2, "=Сценарий_номер", font=S.FONT_NOTE, fmt=S.F_INT,
                 align=S.AL_CENTER)
        for j, (nm, _, fmt) in enumerate(cols):
            self.put(sh, top, 3 + j, f"={nm}", font=S.FONT_NOTE, fmt=fmt, align=S.AL_RIGHT)
        for i in range(3):
            rr = top + 1 + i
            self.put(sh, rr, 1, f"=INDEX({absr(3, hdr)}:{absr(5, hdr)},B{rr})", font=S.FONT_BOLD,
                     border=S.BORDER)
            self.put_service(sh, rr, 2, i + 1, fmt=S.F_INT).alignment = S.AL_CENTER
            for j, (_, _, fmt) in enumerate(cols):
                self.put(sh, rr, 3 + j, None, fmt=fmt, border=S.BORDER, font=S.FONT_BOLD)
        interior = f"C{top + 1}:{CL(2 + len(cols))}{top + 3}"
        ws.cell(top + 1, 3).value = DataTableFormula(ref=interior, dt2D=False, dtr=False,
                                                     r1=f"C{override_row}")
        self.L.scen_table = (top + 1, 3)
        self.L.data_table_ranges.append((sh, interior))
        for j in range(len(cols)):
            if cols[j][0] in ("ЧП_итого", "Рентабельность", "ROI_период"):
                c = CL(3 + j)
                ws.conditional_formatting.add(
                    f"{c}{top + 1}:{c}{top + 3}",
                    CellIsRule(operator="lessThan", formula=["0"], fill=S.FILL_RED))
        r = top + 5
        self.put(sh, r, 1, "Таблица пересчитывает всю модель для каждого сценария. Если в Excel "
                           "включён режим «Автоматически, кроме таблиц данных», нажмите F9.",
                 font=S.FONT_NOTE)

    # ================================================================ Юнит-экономика
    def build_unit(self):
        sh = SH_UNIT
        ws = self.ws[sh]
        K = self.K
        self.title(sh, "Юнит-экономика: на 1 проданную единицу")
        self.put(sh, 3 if self.a.demo_note else 2, 1, '="Сценарий: "&Сценарий_выбор&"   •   курс "&FIXED(Курс,2)&'
                           '" ₽/¥   •   налог: "&Налог_режим', font=S.FONT_SUBTITLE)
        ws.column_dimensions["A"].width = 52
        ws.column_dimensions["B"].width = 10
        for k in range(K):
            ws.column_dimensions[CL(3 + k)].width = 17
        r = 4
        self.header_row(sh, r, ["Показатель", "Ед."])
        for k in range(K):
            self.put(sh, r, 3 + k, f'=""&{self.in_sku("name", k)}', font=S.FONT_HEADER,
                     fill=S.FILL_HEADER, align=S.AL_CENTER, border=S.BORDER)
        ws.row_dimensions[r].height = 30
        self.unit_names_row = r
        U = self.L.unit_rows
        r += 1

        def u(key, k, absolute=False):
            col = CL(3 + k)
            return f"${col}${U[key]}" if absolute else f"{col}{U[key]}"

        def row(key, label, unit, fn, fmt=S.F_RUB_UNIT, bold=False, indent=False, note=False):
            nonlocal r
            U[key] = r
            font = S.FONT_BOLD if bold else (S.FONT_NOTE if note else S.FONT)
            fill = S.FILL_TOTAL if bold else None
            self.put(sh, r, 1, label, font=font, fill=fill,
                     align=S.AL_INDENT if indent else S.AL_LEFT)
            self.put(sh, r, 2, unit, font=S.FONT_NOTE, fill=fill, align=S.AL_CENTER)
            for k in range(K):
                self.put(sh, r, 3 + k, fn(k), font=font, fill=fill, fmt=fmt, border=S.BORDER)
            r += 1

        def sec(text):
            nonlocal r
            self.section(sh, r, text, 1, 2 + K)
            r += 1

        sec("Цена")
        row("price", "Цена продажи с учётом сценария", "₽",
            lambda k: f"={self.in_sku('price_rub', k)}*Множитель_цены", bold=True)
        sec("Себестоимость 1 шт до склада маркетплейса")
        row("price_cny", "Закупочная цена", "¥", lambda k: f"={self.in_sku('price_cny', k)}",
            fmt=S.F_CNY)
        row("fx", "Курс юаня", "₽/¥", lambda k: "=Курс", fmt=S.F_FX)
        row("goods", "Товар", "₽", lambda k: f"={u('price_cny', k)}*{u('fx', k)}")
        row("agent", "Комиссия агента", "₽", lambda k: f"={u('goods', k)}*Комиссия_агента")
        row("bank", "Банк и конвертация", "₽",
            lambda k: f"=({u('goods', k)}+{u('agent', k)})*Банк_комиссия")
        row("freight", "Доставка Китай → РФ", "₽",
            lambda k: (f"=CHOOSE(Доставка_номер,{self.in_sku('weight_kg', k)}*Доставка_кг,"
                       f"{self.in_sku('volume_m3', k)}*Доставка_м3,"
                       f"MAX({self.in_sku('weight_kg', k)}*Доставка_кг,"
                       f"{self.in_sku('volume_m3', k)}*Доставка_м3))"))
        row("customs_value", "Таможенная стоимость (товар + доставка), справочно", "₽",
            lambda k: f"={u('goods', k)}+{u('freight', k)}", note=True, indent=True)
        row("duty", "Пошлина", "₽",
            lambda k: f"={u('customs_value', k)}*{self.in_sku('duty_rate', k)}")
        row("vat", "НДС на ввоз", "₽",
            lambda k: f"=({u('customs_value', k)}+{u('duty', k)})*{self.in_sku('vat_rate', k)}")
        row("customs_fee", "Таможенное оформление на 1 шт", "₽",
            lambda k: f"=IFERROR(Тамож_оформление/{self.in_sku('batch', k)},0)")
        row("pack", "Упаковка и маркировка", "₽", lambda k: f"={self.in_sku('pack_rub', k)}")
        row("inbound", "Доставка до склада маркетплейса", "₽",
            lambda k: f"={self.in_sku('weight_kg', k)}*Доставка_до_МП")
        row("landed", "Себестоимость 1 шт на складе маркетплейса", "₽",
            lambda k: "=" + "+".join(u(x, k) for x in ("goods", "agent", "bank", "freight",
                                                       "duty", "vat", "customs_fee", "pack",
                                                       "inbound")), bold=True)
        row("pay_order", "в т.ч. платится при заказе (товар, агент, банк)", "₽",
            lambda k: f"={u('goods', k)}+{u('agent', k)}+{u('bank', k)}", indent=True, note=True)
        row("pay_arrival", "в т.ч. платится при поступлении (доставка, таможня, подготовка)", "₽",
            lambda k: f"={u('landed', k)}-{u('pay_order', k)}", indent=True, note=True)
        sec("Расходы на продажу, на 1 выкупленную шт")
        row("commission", "Комиссия маркетплейса", "₽",
            lambda k: f"={u('price', k)}*{self.in_sku('mp_commission', k)}")
        row("mp_logistics", "Логистика маркетплейса с учётом возвратов", "₽",
            lambda k: (f"=({self.in_sku('mp_logistics', k)}+{self.in_sku('returns', k)}"
                       f"*Возврат_логистика)/(1-{self.in_sku('returns', k)})"))
        row("ads", "Реклама", "₽", lambda k: f"={u('price', k)}*ДРР")
        row("pretax", "Прибыль до налога", "₽",
            lambda k: (f"={u('price', k)}-{u('landed', k)}-{u('commission', k)}"
                       f"-{u('mp_logistics', k)}-{u('ads', k)}"))
        row("tax", "Налог", "₽",
            lambda k: (f"=CHOOSE(Налог_номер,{u('price', k)}*Ставка_УСН6,"
                       f"MAX({u('pretax', k)}*Ставка_УСН15,{u('price', k)}*Мин_налог_УСН15),"
                       f"{u('price', k)}*Ставка_НПД)"))
        row("full_cost", "Полная себестоимость продажи", "₽",
            lambda k: "=" + "+".join(u(x, k) for x in ("landed", "commission", "mp_logistics",
                                                       "ads", "tax")), bold=True)
        sec("Результат на 1 шт")
        row("profit", "Прибыль на 1 шт", "₽", lambda k: f"={u('price', k)}-{u('full_cost', k)}",
            bold=True)
        row("margin", "Маржа (прибыль / цена)", "%",
            lambda k: f"=IFERROR({u('profit', k)}/{u('price', k)},0)", fmt=S.F_PCT)
        row("markup", "Наценка к себестоимости на складе МП", "%",
            lambda k: f"=IFERROR(({u('price', k)}-{u('landed', k)})/{u('landed', k)},0)",
            fmt=S.F_PCT)
        row("roi", "ROI на вложенный рубль (прибыль / себестоимость)", "%",
            lambda k: f"=IFERROR({u('profit', k)}/{u('landed', k)},0)", fmt=S.F_PCT)
        sec("Безубыточность")
        price_rng = f"${CL(3)}${U['price']}:${CL(2 + K)}${U['price']}"
        plateau_rng = ref(SH_IN, f"${CL(3)}${self.L.in_rows['plateau']}:"
                                 f"${CL(2 + K)}${self.L.in_rows['plateau']}")
        row("plateau_eff", "План продаж на плато с учётом сценария", "шт/мес",
            lambda k: f"={self.in_sku('plateau', k)}*Множитель_продаж", fmt=S.F_QTY1)
        row("rev_share", "Доля товара в выручке на плато", "%",
            lambda k: (f"=IFERROR({u('price', k)}*{self.in_sku('plateau', k)}/"
                       f"SUMPRODUCT({price_rng},{plateau_rng}),0)"), fmt=S.F_PCT)
        row("fixed_alloc", "Постоянные расходы, приходящиеся на товар", "₽/мес",
            lambda k: f"=Пост_расходы*{u('rev_share', k)}", fmt=S.F_RUB)
        row("bep", "Точка безубыточности (покрыть свои постоянные расходы)", "шт/мес",
            lambda k: (f'=IF({u("profit", k)}>0,{u("fixed_alloc", k)}/{u("profit", k)},'
                       f'"убыток на ед.")'), fmt=S.F_QTY1, bold=True)
        row("cert_payback", "Сертификация окупается за", "шт",
            lambda k: (f'=IF({u("profit", k)}>0,{self.in_sku("cert_rub", k)}/{u("profit", k)},'
                       f'"—")'), fmt=S.F_QTY)
        last = f"{CL(2 + K)}"
        for key in ("profit", "margin", "roi", "pretax"):
            rng = f"C{U[key]}:{last}{U[key]}"
            self.ws[sh].conditional_formatting.add(
                rng, CellIsRule(operator="lessThan", formula=["0"], fill=S.FILL_RED,
                                font=Font(color=S.C_RED, bold=True)))
        self.ws[sh].conditional_formatting.add(
            f"C{U['bep']}:{last}{U['bep']}",
            FormulaRule(formula=[f"ISTEXT(C{U['bep']})"], fill=S.FILL_RED,
                        font=Font(color=S.C_RED, bold=True)))
        self.ws[sh].conditional_formatting.add(
            f"C{U['bep']}:{last}{U['bep']}",
            FormulaRule(formula=[f"AND(ISNUMBER(C{U['bep']}),C{U['bep']}>C{U['plateau_eff']})"],
                        fill=S.FILL_RED))
        ws.freeze_panes = "C5"

    def unit_ref(self, key, k, absolute=True):
        col = CL(self.L.unit_col0 + k)
        row = self.L.unit_rows[key]
        return ref(SH_UNIT, f"${col}${row}" if absolute else f"{col}{row}")

    # ================================================================ Закупки и склад
    def build_flow(self):
        sh = SH_FLOW
        K = self.K
        self.title(sh, "Закупки и склад: штуки по месяцам",
                   "Логика заказа: каждый месяц модель проверяет остаток + товар в пути. Если их "
                   "не хватает на продажи за срок поставки + страховой запас — заказывает "
                   "партии (кратно партии SKU). Заказ приходит через «Срок поставки» месяцев.")
        self.month_grid_header(sh)
        F = self.L.flow_rows
        FT = self.L.flow_total_rows
        r = FIRST_DATA_ROW
        # заранее раскладываем строки, чтобы формулы могли ссылаться «вперёд»
        blocks = [("demand", True), ("arrivals", True), ("sales", True), ("lost", True),
                  ("stock", True), ("transit", True), ("need", False), ("batches", False),
                  ("orders", True)]
        plan = {}
        for key, has_total in blocks:
            plan[key] = r            # строка заголовка блока
            F[key] = [r + 1 + k for k in range(K)]
            r += 1 + K
            if has_total:
                FT[key] = r
                r += 1
            if key == "stock":
                FT["stock_value"] = r
                r += 1
            r += 1
        idx = f"${FIRST_M}${ROW_IDX}:${LAST_M}${ROW_IDX}"

        def prev_rng(row, prev):
            return f"${START_L}${row}:{prev}${row}"

        titles = {
            "demand": ("1. Спрос: план выкупов", "шт", "плато, шт/мес"),
            "arrivals": ("2. Поступление на склад маркетплейса", "шт", ""),
            "sales": ("3. Продажи (выкупы) = min(спрос, доступный товар)", "шт", ""),
            "lost": ("4. Упущенные продажи — товара не было", "шт", ""),
            "stock": ("5. Остаток на складе на конец месяца", "шт", "себест. 1 шт, ₽"),
            "transit": ("6. Товар в пути (заказан, ещё не поступил)", "шт", "оплачено за 1 шт, ₽"),
            "need": ("7. Нужно на срок поставки + страховой запас", "шт", ""),
            "batches": ("8. Заказ поставщику, партий", "партий", "партия, шт"),
            "orders": ("9. Заказ поставщику, штук", "шт", "партия, шт"),
        }
        for key, has_total in blocks:
            title, unit, phead = titles[key]
            self.section(sh, plan[key], title)
            self.put(sh, plan[key], COL_PARAM, phead, font=S.FONT_NOTE, fill=S.FILL_SECTION)
            for k in range(K):
                row = F[key][k]
                name = f'=""&{self.in_sku("name", k)}'
                dem, arr, sal = F["demand"][k], F["arrivals"][k], F["sales"][k]
                stk, trn, ned = F["stock"][k], F["transit"][k], F["need"][k]
                bat, ordr = F["batches"][k], F["orders"][k]
                kw = dict(indent=True, fmt=S.F_QTY)
                if key == "demand":
                    self.month_row(sh, row, name, unit,
                                   lambda t, c, p, row=row: (
                                       f"=ROUND($C{row}*INDEX(Темп_продаж,{c}${ROW_IDX})"
                                       f"*Множитель_продаж,0)"),
                                   param=f"={self.in_sku('plateau', k)}", param_fmt=S.F_QTY, **kw)
                elif key == "arrivals":
                    self.month_row(sh, row, name, unit,
                                   lambda t, c, p: (f"=SUMIFS({prev_rng(ordr, p)},"
                                                    f"${START_L}${ROW_IDX}:{p}${ROW_IDX},"
                                                    f"{c}${ROW_IDX}-Срок_поставки)"), **kw)
                elif key == "sales":
                    self.month_row(sh, row, name, unit,
                                   lambda t, c, p: f"=MIN({c}{dem},{p}{stk}+{c}{arr})", **kw)
                elif key == "lost":
                    self.month_row(sh, row, name, unit,
                                   lambda t, c, p: f"={c}{dem}-{c}{sal}", **kw)
                elif key == "stock":
                    self.month_row(sh, row, name, unit,
                                   lambda t, c, p: f"={p}{stk}+{c}{arr}-{c}{sal}", total=None,
                                   param=f"={self.unit_ref('landed', k)}", param_fmt=S.F_RUB,
                                   start=f"={self.in_sku('start_stock', k)}", **kw)
                elif key == "transit":
                    self.month_row(sh, row, name, unit,
                                   lambda t, c, p: (f"=SUMIFS({prev_rng(ordr, p)},"
                                                    f"${START_L}${ROW_IDX}:{p}${ROW_IDX},"
                                                    f'">"&({c}${ROW_IDX}-Срок_поставки))'),
                                   total=None, param=f"={self.unit_ref('pay_order', k)}",
                                   param_fmt=S.F_RUB, **kw)
                elif key == "need":
                    self.month_row(sh, row, name, unit,
                                   lambda t, c, p: (
                                       f"=SUMIFS(${FIRST_M}{dem}:${LAST_M}{dem},{idx},"
                                       f'">"&{c}${ROW_IDX},{idx},'
                                       f'"<="&({c}${ROW_IDX}+Срок_поставки+Страх_запас))'
                                       f"+MAX(0,{c}${ROW_IDX}+Срок_поставки+Страх_запас"
                                       f"-${LAST_M}${ROW_IDX})*${LAST_M}{dem}"),
                                   total=None, **kw)
                elif key == "batches":
                    self.month_row(sh, row, name, unit,
                                   lambda t, c, p, row=row: (
                                       f"=IF({c}{ned}-{c}{stk}-{c}{trn}>0,"
                                       f"ROUNDUP(({c}{ned}-{c}{stk}-{c}{trn})/$C{row},0),0)"),
                                   param=f"={self.in_sku('batch', k)}", param_fmt=S.F_QTY, **kw)
                elif key == "orders":
                    self.month_row(sh, row, name, unit,
                                   lambda t, c, p, row=row: f"={c}{bat}*$C{row}",
                                   param=f"={self.in_sku('batch', k)}", param_fmt=S.F_QTY, **kw)
            if has_total:
                r1, rK = F[key][0], F[key][-1]
                self.month_row(sh, FT[key], "Итого", unit,
                               lambda t, c, p: f"=SUM({c}{r1}:{c}{rK})", bold=True, fmt=S.F_QTY,
                               total=None if key in ("stock", "transit") else "sum",
                               start=(f"=SUM({START_L}{r1}:{START_L}{rK})"
                                      if key == "stock" else None))
            if key == "stock":
                r1, rK = F["stock"][0], F["stock"][-1]
                self.month_row(sh, FT["stock_value"], "Стоимость запаса по себестоимости", "₽",
                               lambda t, c, p: f"=SUMPRODUCT({c}{r1}:{c}{rK},$C{r1}:$C{rK})",
                               bold=True, total=None,
                               start=f"=SUMPRODUCT({START_L}{r1}:{START_L}{rK},$C{r1}:$C{rK})")
        ws = self.ws[sh]
        rng = f"{FIRST_M}{FT['lost']}:{LAST_M}{FT['lost']}"
        ws.conditional_formatting.add(rng, CellIsRule(operator="greaterThan", formula=["0"],
                                                      fill=S.FILL_RED))
        for k in range(K):
            rr = F["orders"][k]
            ws.conditional_formatting.add(
                f"{FIRST_M}{rr}:{LAST_M}{rr}",
                CellIsRule(operator="greaterThan", formula=["0"], fill=S.FILL_GREEN,
                           font=Font(bold=True, color=S.C_GREEN)))

    def flow_ref(self, key, k, col_letter, absolute_row=True):
        row = self.L.flow_rows[key][k]
        return ref(SH_FLOW, f"{col_letter}${row}" if absolute_row else f"{col_letter}{row}")

    # ================================================================ ОПиУ и ДДС
    def plan_pnl_cash_rows(self):
        """Раскладка строк ОПиУ и ДДС до записи формул (листы ссылаются друг на друга)."""
        K = self.K
        P, PS = self.L.pnl_rows, self.L.pnl_sku_rows
        r = FIRST_DATA_ROW + 1          # строка 6 — год, 7 — пусто/заголовок
        P["year"] = ROW_DATE + 1
        order = ["revenue*", "cogs*", "gross", "gross_margin", "commission*", "mp_logistics*",
                 "ads", "fixed", "startup", "cert", "pretax", "ytd_revenue", "ytd_base",
                 "ytd_tax", "tax", "net", "net_cum", "net_margin"]
        for item in order:
            if item.endswith("*"):
                key = item[:-1]
                P[key] = r
                PS[key] = [r + 1 + k for k in range(K)]
                r += 1 + K
            else:
                P[item] = r
                r += 1
            if item in ("gross_margin", "pretax", "ytd_tax", "net_margin"):
                r += 1
        P["_annual_start"] = r + 1
        C, CS = self.L.cash_rows, self.L.cash_sku_rows
        r = FIRST_DATA_ROW
        C["opening"] = r
        r += 2
        C["_in_section"] = r
        r += 1
        C["lag"] = r
        C["frac"] = r + 1
        C["payout_base"] = r + 2
        C["payout"] = r + 3
        r += 5
        C["_out_section"] = r
        r += 1
        for key in ("pay_order", "freight", "duty", "vat", "customs_fee", "prep", "cert"):
            C[key] = r
            CS[key] = [r + 1 + k for k in range(K)]
            r += 1 + K
        for key in ("ads", "fixed", "startup", "tax_paid", "outflow"):
            C[key] = r
            r += 1
        r += 1
        for key in ("net_cf", "closing", "gap", "gap_flag", "cum_cf", "min_future",
                    "paid_back", "cash_pos", "cash_neg"):
            C[key] = r
            r += 1
        C["_end_section"] = r + 1

    def build_pnl(self):
        sh = SH_PNL
        K = self.K
        P, PS = self.L.pnl_rows, self.L.pnl_sku_rows
        self.title(sh, "ОПиУ — отчёт о прибылях и убытках по месяцам (метод начисления)",
                   "Выручка и себестоимость — по месяцу выкупа. Налог УСН 15 % считается "
                   "нарастающим итогом с начала календарного года.")
        self.month_grid_header(sh)
        self.month_row(sh, P["year"], "Год (служебная)", "", lambda t, c, p: f"=YEAR({c}${ROW_DATE})",
                       fmt=S.F_INT, total=None, service=True)
        idx = f"${FIRST_M}${ROW_IDX}:${LAST_M}${ROW_IDX}"
        yr = f"${FIRST_M}${P['year']}:${LAST_M}${P['year']}"

        def sku_block(key, label, param_fn, cell_fn, param_fmt, fmt=S.F_RUB):
            rows = PS[key]
            self.month_row(sh, P[key], label, "₽",
                           lambda t, c, p: f"=SUM({c}{rows[0]}:{c}{rows[-1]})", bold=True,
                           fmt=fmt)
            for k in range(K):
                row = rows[k]
                self.month_row(sh, row, f'=""&{self.in_sku("name", k)}', "₽",
                               lambda t, c, p, k=k, row=row: cell_fn(k, c, row), indent=True,
                               param=param_fn(k), param_fmt=param_fmt, fmt=fmt)

        sku_block("revenue", "Выручка", lambda k: f"={self.unit_ref('price', k)}",
                  lambda k, c, row: f"={self.flow_ref('sales', k, c)}*$C{row}", S.F_RUB)
        sku_block("cogs", "Себестоимость продаж", lambda k: f"={self.unit_ref('landed', k)}",
                  lambda k, c, row: f"={self.flow_ref('sales', k, c)}*$C{row}", S.F_RUB)
        self.month_row(sh, P["gross"], "Валовая прибыль", "₽",
                       lambda t, c, p: f"={c}{P['revenue']}-{c}{P['cogs']}", bold=True)
        self.month_row(sh, P["gross_margin"], "Валовая маржа", "%",
                       lambda t, c, p: f"=IFERROR({c}{P['gross']}/{c}{P['revenue']},0)",
                       fmt=S.F_PCT, total=None)
        self.put(sh, P["gross_margin"], COL_TOTAL,
                 f"=IFERROR(D{P['gross']}/D{P['revenue']},0)", font=S.FONT_BOLD,
                 fill=S.FILL_TOTAL, fmt=S.F_PCT)
        sku_block("commission", "Комиссия маркетплейса",
                  lambda k: f"={self.in_sku('mp_commission', k)}",
                  lambda k, c, row: f"={c}{PS['revenue'][k]}*$C{row}", S.F_PCT)
        sku_block("mp_logistics", "Логистика маркетплейса (с возвратами)",
                  lambda k: f"={self.unit_ref('mp_logistics', k)}",
                  lambda k, c, row: f"={self.flow_ref('sales', k, c)}*$C{row}", S.F_RUB_UNIT)
        self.month_row(sh, P["ads"], "Реклама", "₽", lambda t, c, p: f"={c}{P['revenue']}*$C{P['ads']}",
                       param="=ДРР", param_fmt=S.F_PCT)
        self.month_row(sh, P["fixed"], "Постоянные расходы", "₽",
                       lambda t, c, p: f"=$C{P['fixed']}", param="=Пост_расходы",
                       param_fmt=S.F_RUB)
        self.month_row(sh, P["startup"], "Стартовые расходы", "₽",
                       lambda t, c, p: f"=IF({c}${ROW_IDX}=1,$C{P['startup']},0)",
                       param="=Старт_расходы", param_fmt=S.F_RUB)
        cert_row = self.L.cash_rows["cert"]
        self.month_row(sh, P["cert"], "Сертификация", "₽",
                       lambda t, c, p: f"={ref(SH_CASH, f'{c}${cert_row}')}")
        self.month_row(sh, P["pretax"], "Прибыль до налога", "₽",
                       lambda t, c, p: (f"={c}{P['gross']}-{c}{P['commission']}"
                                        f"-{c}{P['mp_logistics']}-{c}{P['ads']}-{c}{P['fixed']}"
                                        f"-{c}{P['startup']}-{c}{P['cert']}"), bold=True)
        self.month_row(sh, P["ytd_revenue"], "Доходы с начала года (служебная)", "₽",
                       lambda t, c, p: (f"=SUMIFS(${FIRST_M}{P['revenue']}:${LAST_M}{P['revenue']},"
                                        f'{yr},{c}${P["year"]},{idx},"<="&{c}${ROW_IDX})'),
                       total=None, service=True)
        self.month_row(sh, P["ytd_base"], "Прибыль до налога с начала года (служебная)", "₽",
                       lambda t, c, p: (f"=SUMIFS(${FIRST_M}{P['pretax']}:${LAST_M}{P['pretax']},"
                                        f'{yr},{c}${P["year"]},{idx},"<="&{c}${ROW_IDX})'),
                       total=None, service=True)
        self.month_row(sh, P["ytd_tax"], "Налог с начала года (служебная)", "₽",
                       lambda t, c, p: (f"=CHOOSE(Налог_номер,Ставка_УСН6*{c}{P['ytd_revenue']},"
                                        f"MAX(Ставка_УСН15*{c}{P['ytd_base']},"
                                        f"Мин_налог_УСН15*{c}{P['ytd_revenue']}),"
                                        f"Ставка_НПД*{c}{P['ytd_revenue']})"),
                       total=None, service=True)
        self.month_row(sh, P["tax"], "Налог (начислен за месяц)", "₽",
                       lambda t, c, p: (f"={c}{P['ytd_tax']}-IF({p}${P['year']}={c}${P['year']},"
                                        f"{p}{P['ytd_tax']},0)"))
        self.month_row(sh, P["net"], "Чистая прибыль", "₽",
                       lambda t, c, p: f"={c}{P['pretax']}-{c}{P['tax']}", bold=True)
        self.month_row(sh, P["net_cum"], "Чистая прибыль нарастающим итогом", "₽",
                       lambda t, c, p: f"={p}{P['net_cum']}+{c}{P['net']}", total=None)
        self.month_row(sh, P["net_margin"], "Рентабельность по чистой прибыли", "%",
                       lambda t, c, p: f"=IFERROR({c}{P['net']}/{c}{P['revenue']},0)",
                       fmt=S.F_PCT, total=None)
        self.put(sh, P["net_margin"], COL_TOTAL, f"=IFERROR(D{P['net']}/D{P['revenue']},0)",
                 font=S.FONT_BOLD, fill=S.FILL_TOTAL, fmt=S.F_PCT)
        ws = self.ws[sh]
        for key in ("net", "pretax"):
            ws.conditional_formatting.add(
                f"{FIRST_M}{P[key]}:{LAST_M}{P[key]}",
                CellIsRule(operator="lessThan", formula=["0"], font=Font(color=S.C_RED, bold=True)))
        # выручка по календарным годам — для предупреждений о лимитах
        r = P["_annual_start"]
        self.section(sh, r, "Выручка по календарным годам (для проверки лимитов НПД и порога НДС)",
                     1, 5)
        r += 1
        for i in range(3):
            self.put(sh, r, 1, "Выручка за календарный год", align=S.AL_INDENT)
            self.put(sh, r, 2, "год", font=S.FONT_NOTE, align=S.AL_CENTER)
            self.put(sh, r, 3, f"=YEAR(Дата_начала)+{i}", fmt=S.F_INT)
            self.put(sh, r, 4, f"=SUMIFS(${FIRST_M}${P['revenue']}:${LAST_M}${P['revenue']},"
                               f"{yr},$C{r})", fmt=S.F_RUB, font=S.FONT_BOLD)
            self.L.annual_rows.append(r)
            r += 1
        self.put(sh, r, 1, "Максимум за календарный год", font=S.FONT_BOLD)
        self.put(sh, r, 4, f"=MAX(D{self.L.annual_rows[0]}:D{self.L.annual_rows[-1]})",
                 fmt=S.F_RUB, font=S.FONT_BOLD, fill=S.FILL_TOTAL)
        self.name("Выручка_макс_год", sh, absr(4, r))

    def build_cash(self):
        sh = SH_CASH
        K = self.K
        C, CS = self.L.cash_rows, self.L.cash_sku_rows
        P = self.L.pnl_rows
        self.title(sh, "Денежный поток по месяцам",
                   "Покупка товара — деньги уходят при заказе (товар) и при поступлении "
                   "(доставка, таможня). Выручка приходит с задержкой выплат маркетплейса. "
                   "Красным — месяцы, когда денег не хватает (кассовый разрыв).")
        self.month_grid_header(sh)
        idx = f"${FIRST_M}${ROW_IDX}:${LAST_M}${ROW_IDX}"
        self.month_row(sh, C["opening"], "Остаток денег на начало месяца", "₽",
                       lambda t, c, p: f"={p}{C['closing']}", total=None, bold=True)
        self.section(sh, C["_in_section"], "ПОСТУПЛЕНИЯ")
        # задержка выплат
        self.put(sh, C["lag"], 1, "Задержка выплат: целых месяцев (служебная)",
                 font=S.FONT_SERVICE)
        self.put(sh, C["lag"], COL_PARAM, "=INT(Задержка_выплат_дн/Дней_в_месяце)",
                 font=S.FONT_SERVICE, fmt=S.F_INT)
        self.put(sh, C["frac"], 1, "Доля выплат, которая приходит ещё на месяц позже (служебная)",
                 font=S.FONT_SERVICE)
        self.put(sh, C["frac"], COL_PARAM,
                 f"=Задержка_выплат_дн/Дней_в_месяце-$C${C['lag']}", font=S.FONT_SERVICE,
                 fmt=S.F_PCT)
        self.L.cash_cells["lag"] = f"C{C['lag']}"
        self.L.cash_cells["frac"] = f"C{C['frac']}"
        pb = C["payout_base"]
        self.month_row(sh, pb, "Начислено маркетплейсом к выплате (выручка − комиссия − логистика)",
                       "₽", lambda t, c, p: (f"={ref(SH_PNL, f'{c}{P.revenue}')}"
                                             f"-{ref(SH_PNL, f'{c}{P.commission}')}"
                                             f"-{ref(SH_PNL, f'{c}{P.mp_logistics}')}"),
                       service=True)
        lag, frac = f"$C${C['lag']}", f"$C${C['frac']}"
        base_rng = f"${FIRST_M}${pb}:${LAST_M}${pb}"
        self.month_row(sh, C["payout"], "Выплаты маркетплейса", "₽",
                       lambda t, c, p: (f"=(1-{frac})*SUMIFS({base_rng},{idx},{c}${ROW_IDX}-{lag})"
                                        f"+{frac}*SUMIFS({base_rng},{idx},{c}${ROW_IDX}-{lag}-1)"),
                       bold=True)
        self.section(sh, C["_out_section"], "ВЫПЛАТЫ")
        blocks = [
            ("pay_order", "Оплата поставщику (товар + агент + банк)", "orders", "pay_order"),
            ("freight", "Доставка из Китая", "arrivals", "freight"),
            ("duty", "Пошлина", "arrivals", "duty"),
            ("vat", "НДС на ввоз", "arrivals", "vat"),
            ("customs_fee", "Таможенное оформление", "arrivals", "customs_fee"),
            ("prep", "Упаковка, маркировка, доставка до склада МП", "arrivals", None),
            ("cert", "Сертификация (в месяц первого заказа)", "orders", None),
        ]
        for key, label, units_key, unit_key in blocks:
            rows = CS[key]
            self.month_row(sh, C[key], label, "₽",
                           lambda t, c, p, rows=rows: f"=SUM({c}{rows[0]}:{c}{rows[-1]})",
                           bold=True)
            for k in range(K):
                row = rows[k]
                if key == "prep":
                    param = f"={self.unit_ref('pack', k)}+{self.unit_ref('inbound', k)}"
                elif key == "cert":
                    param = f"={self.in_sku('cert_rub', k)}"
                else:
                    param = f"={self.unit_ref(unit_key, k)}"
                if key == "cert":
                    ordr = self.L.flow_rows["orders"][k]

                    def fn(t, c, p, row=row, ordr=ordr):
                        return (f"=IF(AND({ref(SH_FLOW, f'{c}{ordr}')}>0,"
                                f"SUM({ref(SH_FLOW, f'${START_L}{ordr}:{p}{ordr}')})=0),$C{row},0)")
                else:
                    def fn(t, c, p, row=row, k=k, units_key=units_key):
                        return f"={self.flow_ref(units_key, k, c)}*$C{row}"
                self.month_row(sh, row, f'=""&{self.in_sku("name", k)}', "₽", fn, indent=True,
                               param=param, param_fmt=S.F_RUB_UNIT)
        self.month_row(sh, C["ads"], "Реклама", "₽",
                       lambda t, c, p: f"={ref(SH_PNL, f'{c}{P.ads}')}", bold=True)
        self.month_row(sh, C["fixed"], "Постоянные расходы", "₽",
                       lambda t, c, p: f"={ref(SH_PNL, f'{c}{P.fixed}')}", bold=True)
        self.month_row(sh, C["startup"], "Стартовые расходы", "₽",
                       lambda t, c, p: f"={ref(SH_PNL, f'{c}{P.startup}')}", bold=True)
        tax_rng = ref(SH_PNL, f"${FIRST_M}${P['tax']}:${LAST_M}${P['tax']}")
        pidx = ref(SH_PNL, idx)
        self.month_row(sh, C["tax_paid"], "Налог (уплата: УСН — поквартально, НПД — ежемесячно)",
                       "₽", lambda t, c, p: (
                           f"=IF(Налог_номер=3,SUMIFS({tax_rng},{pidx},{c}${ROW_IDX}-1),"
                           f"IF(MOD(MONTH({c}${ROW_DATE}),3)=1,SUMIFS({tax_rng},{pidx},"
                           f'">="&({c}${ROW_IDX}-3),{pidx},"<"&{c}${ROW_IDX}),0))'), bold=True)
        out_keys = ["pay_order", "freight", "duty", "vat", "customs_fee", "prep", "cert", "ads",
                    "fixed", "startup", "tax_paid"]
        self.month_row(sh, C["outflow"], "Итого выплаты", "₽",
                       lambda t, c, p: "=" + "+".join(f"{c}{C[k]}" for k in out_keys), bold=True,
                       fill=S.FILL_SECTION)
        self.month_row(sh, C["net_cf"], "Чистый денежный поток за месяц", "₽",
                       lambda t, c, p: f"={c}{C['payout']}-{c}{C['outflow']}", bold=True)
        self.month_row(sh, C["closing"], "Остаток денег на конец месяца", "₽",
                       lambda t, c, p: f"={p}{C['closing']}+{c}{C['net_cf']}", bold=True,
                       total=None, start="=Капитал_старт")
        self.month_row(sh, C["gap"], "Кассовый разрыв: не хватает денег", "₽",
                       lambda t, c, p: f"=MAX(0,-{c}{C['closing']})", total=None)
        self.put(sh, C["gap"], COL_TOTAL, f"=MAX({FIRST_M}{C['gap']}:{LAST_M}{C['gap']})",
                 font=S.FONT_BOLD, fill=S.FILL_TOTAL, fmt=S.F_RUB)
        self.put(sh, C["gap"], COL_PARAM, "макс. →", font=S.FONT_NOTE, align=S.AL_RIGHT)
        self.month_row(sh, C["gap_flag"], "Разрыв в этом месяце? (служебная)", "",
                       lambda t, c, p: f"={c}{C['closing']}<0", total=None, service=True,
                       fmt="General")
        self.month_row(sh, C["cum_cf"], "Накопленный денежный поток (без стартового капитала)", "₽",
                       lambda t, c, p: f"={p}{C['cum_cf']}+{c}{C['net_cf']}", total=None)
        self.month_row(sh, C["min_future"], "Минимум накопленного потока до конца (служебная)",
                       "₽", lambda t, c, p: f"=MIN({c}{C['cum_cf']}:${LAST_M}{C['cum_cf']})",
                       total=None, service=True)
        self.month_row(sh, C["paid_back"], "Вложения окупились и больше не уходят в минус? "
                                           "(служебная)", "",
                       lambda t, c, p: f"={c}{C['min_future']}>=0", total=None, service=True,
                       fmt="General")
        self.month_row(sh, C["cash_pos"], "Для графика: остаток ≥ 0 (служебная)", "₽",
                       lambda t, c, p: f"=MAX({c}{C['closing']},0)", total=None, service=True)
        self.month_row(sh, C["cash_neg"], "Для графика: остаток < 0 (служебная)", "₽",
                       lambda t, c, p: f"=MIN({c}{C['closing']},0)", total=None, service=True)
        ws = self.ws[sh]
        rng = f"{FIRST_M}{C['closing']}:{LAST_M}{C['closing']}"
        ws.conditional_formatting.add(rng, CellIsRule(
            operator="lessThan", formula=["0"], fill=S.FILL_RED,
            font=Font(color=S.C_RED, bold=True)))
        rng = f"{FIRST_M}{C['gap']}:{LAST_M}{C['gap']}"
        ws.conditional_formatting.add(rng, CellIsRule(
            operator="greaterThan", formula=["0"], fill=S.FILL_RED,
            font=Font(color=S.C_RED, bold=True)))

        # ---- итоги на конец периода и контроль баланса
        r = C["_end_section"]
        self.section(sh, r, "Итоги на конец периода и контроль баланса модели", 1, 5)
        r += 1
        FT = self.L.flow_total_rows
        FR = self.L.flow_rows
        tr1, trK = FR["transit"][0], FR["transit"][-1]
        st1, stK = FR["stock"][0], FR["stock"][-1]
        E = self.L.end_cells
        items = [
            ("cash", "Деньги на счёте", f"={LAST_M}{C['closing']}"),
            ("stock_value", "Товар на складе маркетплейса (по себестоимости)",
             f"={ref(SH_FLOW, f'{LAST_M}{FT.stock_value}')}"),
            ("transit_value", "Товар в пути (уже оплачен поставщику)",
             f"=SUMPRODUCT({ref(SH_FLOW, f'{LAST_M}{tr1}:{LAST_M}{trK}')},"
             f"{ref(SH_FLOW, f'$C{tr1}:$C{trK}')})+{LAST_M}{C['pay_order']}"),
            ("receivable", "Маркетплейс ещё должен выплатить", f"=D{pb}-D{C['payout']}"),
            ("tax_payable", "Налог начислен, но ещё не уплачен",
             f"={ref(SH_PNL, f'D{P.tax}')}-D{C['tax_paid']}"),
            ("equity", "Капитал на конец периода (деньги + товар + дебиторка − налог)", None),
            ("start_stock_value", "Начальный запас по себестоимости",
             f"=SUMPRODUCT({ref(SH_FLOW, f'{START_L}{st1}:{START_L}{stK}')},"
             f"{ref(SH_FLOW, f'$C{st1}:$C{stK}')})"),
            ("equity_expected", "Стартовый капитал + начальный запас + чистая прибыль", None),
            ("balance_diff", "Разница (должна быть 0)", None),
            ("balance_ok", "Контроль баланса", None),
        ]
        for key, label, formula in items:
            E[key] = f"D{r}"
            r += 1
        E_ = {k: v for k, v in E.items()}
        formulas = {
            "equity": f"={E_['cash']}+{E_['stock_value']}+{E_['transit_value']}"
                      f"+{E_['receivable']}-{E_['tax_payable']}",
            "equity_expected": f"=Капитал_старт+{E_['start_stock_value']}"
                               f"+{ref(SH_PNL, f'D{P.net}')}",
            "balance_diff": f"={E_['equity']}-{E_['equity_expected']}",
            "balance_ok": f'=IF(ROUND({E_["balance_diff"]},2)=0,"OK","Ошибка")',
        }
        for key, label, formula in items:
            row = int(E[key][1:])
            bold = key in ("equity", "balance_ok")
            self.put(sh, row, 1, label, font=S.FONT_BOLD if bold else S.FONT,
                     align=S.AL_INDENT if not bold else S.AL_LEFT)
            self.put(sh, row, 2, "" if key == "balance_ok" else "₽", font=S.FONT_NOTE,
                     align=S.AL_CENTER)
            self.put(sh, row, COL_TOTAL, formula or formulas[key],
                     font=S.FONT_BOLD if bold else S.FONT,
                     fill=S.FILL_TOTAL if bold else None,
                     fmt=None if key == "balance_ok" else S.F_RUB,
                     align=S.AL_CENTER if key == "balance_ok" else None, border=S.BORDER)
        ok = E["balance_ok"]
        ws.conditional_formatting.add(ok, CellIsRule(operator="equal", formula=['"OK"'],
                                                     fill=S.FILL_GREEN,
                                                     font=Font(color=S.C_GREEN, bold=True)))
        ws.conditional_formatting.add(ok, CellIsRule(operator="notEqual", formula=['"OK"'],
                                                     fill=S.FILL_RED,
                                                     font=Font(color=S.C_RED, bold=True)))
        self.name("Контроль_баланса", sh, "$" + ok[0] + "$" + ok[1:])

    # ================================================================ Чувствительность
    def build_sensitivity(self):
        sh = SH_SENS
        a = self.a
        ws = self.ws[sh]
        self.title(sh, "Чувствительность: как меняется результат от курса, цены и объёма",
                   "Таблицы данных Excel пересчитывают всю модель для каждой клетки (от активного "
                   "сценария; курс в таблице заменяет курс сценария). Оси — жёлтые, их можно "
                   "менять. Если включён режим «Автоматически, кроме таблиц данных» — нажмите F9.")
        ws.column_dimensions["A"].width = 4
        ws.column_dimensions["B"].width = 44
        ws.column_dimensions["C"].width = 16
        for col in "DEFGHIJ":
            ws.column_dimensions[col].width = 15
        r = 5
        self.section(sh, r, "Служебные ячейки анализа (не менять, должны быть 0)", 2, 10)
        r += 1
        svc = {}
        for nm, label, fmt in (("Курс_анализ", "Курс для анализа, ₽/¥ (0 = курс сценария)", S.F_FX),
                               ("Цена_анализ", "Изменение цены для анализа", S.F_PCT_DELTA),
                               ("Объём_анализ", "Изменение объёма продаж для анализа",
                                S.F_PCT_DELTA)):
            self.put(sh, r, 2, label, font=S.FONT_SERVICE)
            self.put_service(sh, r, 3, 0, fmt=fmt)
            self.name(nm, sh, absr(3, r))
            svc[nm] = f"C{r}"
            r += 1
        r += 1
        tables = [
            ("price", "1. Чистая прибыль за 18 мес, ₽: курс юаня × изменение цены продажи",
             "ЧП_итого", a.sens_price, "Цена_анализ", "цена →", False),
            ("sales", "2. Чистая прибыль за 18 мес, ₽: курс юаня × изменение объёма продаж",
             "ЧП_итого", a.sens_sales, "Объём_анализ", "объём →", False),
            ("peak", "3. Потребность в деньгах (пик вложений), ₽: курс юаня × изменение объёма",
             "Пик_вложений", a.sens_sales, "Объём_анализ", "объём →", True),
        ]
        for kind, title, output, col_vals, row_input, axis_label, reverse in tables:
            self.section(sh, r, title, 2, 10)
            r += 1
            corner = r
            self.put(sh, corner, 2, f"Курс ↓ / {axis_label}", font=S.FONT_NOTE, align=S.AL_RIGHT)
            self.put(sh, corner, 3, f"={output}", font=S.FONT_NOTE,
                     fmt=';'.join([f'"курс ↓   {axis_label}"'] * 3), align=S.AL_CENTER, fill=S.FILL_TOTAL,
                     border=S.BORDER)
            for j, v in enumerate(col_vals):
                self.put_input(sh, corner, 4 + j, v, fmt=S.F_PCT_DELTA, bold=True).alignment = \
                    S.AL_CENTER
            for i, fx in enumerate(a.sens_fx):
                self.put(sh, corner + 1 + i, 2, "₽ за ¥" if i == 0 else None, font=S.FONT_NOTE,
                         align=S.AL_RIGHT)
                self.put_input(sh, corner + 1 + i, 3, fx, fmt=S.F_FX, bold=True).alignment = \
                    S.AL_CENTER
                for j in range(len(col_vals)):
                    self.put(sh, corner + 1 + i, 4 + j, None, fmt=S.F_RUB, border=S.BORDER)
            interior = f"D{corner + 1}:{CL(3 + len(col_vals))}{corner + len(a.sens_fx)}"
            ws.cell(corner + 1, 4).value = DataTableFormula(
                ref=interior, dt2D=True, dtr=True, r1=svc[row_input], r2=svc["Курс_анализ"])
            start, end = ("F8696B", "63BE7B") if not reverse else ("63BE7B", "F8696B")
            ws.conditional_formatting.add(interior, ColorScaleRule(
                start_type="min", start_color=start, mid_type="percentile", mid_value=50,
                mid_color="FFEB84", end_type="max", end_color=end))
            self.L.sens[kind] = (corner + 1, 4)
            self.L.data_table_ranges.append((sh, interior))
            r = corner + len(a.sens_fx) + 3

    # ================================================================ Дашборд
    def build_dashboard(self):
        sh = SH_DASH
        a = self.a
        ws = self.ws[sh]
        K = self.K
        C = self.L.cash_rows
        P = self.L.pnl_rows
        FT = self.L.flow_total_rows
        U = self.L.unit_rows
        ws.column_dimensions["A"].width = 2
        for col in range(2, 14):
            ws.column_dimensions[CL(col)].width = 13.5
        ws.column_dimensions["B"].width = 15
        self.put(sh, 1, 2, a.title, font=S.FONT_TITLE)
        # строка параметров
        self.put(sh, 2, 2, "Сценарий:", font=S.FONT_NOTE, align=S.AL_RIGHT)
        self.put(sh, 2, 3, "=Сценарий_выбор", font=S.FONT_BOLD)
        self.put(sh, 2, 5, "Налог:", font=S.FONT_NOTE, align=S.AL_RIGHT)
        self.put(sh, 2, 6, "=Налог_режим", font=S.FONT_BOLD)
        self.put(sh, 2, 8, "Период:", font=S.FONT_NOTE, align=S.AL_RIGHT)
        self.put(sh, 2, 9, "=Дата_начала", font=S.FONT_BOLD, fmt=S.F_DATE_MONTH, align=S.AL_LEFT)
        self.put(sh, 2, 11, "по", font=S.FONT_NOTE, align=S.AL_RIGHT)
        self.put(sh, 2, 12, f"={ref(SH_CASH, f'{LAST_M}${ROW_DATE}')}", font=S.FONT_BOLD,
                 fmt=S.F_DATE_MONTH, align=S.AL_LEFT)
        self.put(sh, 3, 2, a.demo_note, font=S.FONT_DEMO)

        cash_rng = lambda key: ref(SH_CASH, f"${FIRST_M}${C[key]}:${LAST_M}${C[key]}")
        kpis = [
            # (имя, подпись, формула, формат, подстрочник)
            ("Выручка_итого", "ВЫРУЧКА ЗА ПЕРИОД", f"={ref(SH_PNL, f'D{P.revenue}')}",
             S.F_RUB_KPI, "выкупы × цена, 18 мес"),
            ("ЧП_итого", "ЧИСТАЯ ПРИБЫЛЬ", f"={ref(SH_PNL, f'D{P.net}')}", S.F_RUB_KPI,
             "после налога, 18 мес"),
            ("Рентабельность", "РЕНТАБЕЛЬНОСТЬ ПРОДАЖ", "=IFERROR(ЧП_итого/Выручка_итого,0)",
             S.F_PCT, "чистая прибыль / выручка"),
            ("ROI_период", "ROI НА ВЛОЖЕННЫЕ ДЕНЬГИ", "=IFERROR(ЧП_итого/Пик_вложений,0)",
             "0%;[Red]-0%", "чистая прибыль / пик вложений"),
            ("Пик_вложений", "ПОТРЕБНОСТЬ В ДЕНЬГАХ", f"=MAX(0,-MIN({cash_rng('cum_cf')}))",
             S.F_RUB_KPI, "пик вложенных денег"),
            ("Макс_разрыв", "МАКС. КАССОВЫЙ РАЗРЫВ", f"=MAX({cash_rng('gap')})", S.F_RUB_KPI,
             None),
            ("Окупаемость_мес", "ОКУПАЕМОСТЬ ВЛОЖЕНИЙ",
             f'=IFERROR(MATCH(TRUE,{cash_rng("paid_back")},0),'
             f'"более "&MAX({ref(SH_CASH, f"${FIRST_M}${ROW_IDX}:${LAST_M}${ROW_IDX}")}))',
             '0" мес";;;@', "месяц, с которого деньги не уходят в минус"),
            ("Капитал_конец", "КАПИТАЛ НА КОНЕЦ", f"={ref(SH_CASH, self.L.end_cells['equity'])}",
             S.F_RUB_KPI, "деньги + товар + дебиторка − налог"),
        ]
        tiles = [(5, 2), (5, 5), (5, 8), (5, 11), (9, 2), (9, 5), (9, 8), (9, 11)]
        for (nm, label, formula, fmt, sub), (row, col) in zip(kpis, tiles):
            for rr in range(row, row + 3):
                for cc in range(col, col + 3):
                    self.put(sh, rr, cc, None, fill=S.FILL_TILE)
                ws.merge_cells(start_row=rr, start_column=col, end_row=rr, end_column=col + 2)
            self.put(sh, row, col, label, font=S.FONT_KPI_LABEL, fill=S.FILL_TILE,
                     align=S.AL_CENTER)
            self.put(sh, row + 1, col, formula, font=S.FONT_KPI, fill=S.FILL_TILE, fmt=fmt,
                     align=S.AL_CENTER)
            self.name(nm, sh, absr(col, row + 1))
            if sub:
                self.put(sh, row + 2, col, sub, font=S.FONT_NOTE, fill=S.FILL_TILE,
                         align=S.AL_CENTER)
            if nm == "Макс_разрыв":
                self.put(sh, row + 2, col,
                         f"=IFERROR(INDEX({ref(SH_CASH, f'${FIRST_M}${ROW_DATE}:${LAST_M}${ROW_DATE}')},"
                         f"MATCH(TRUE,{cash_rng('gap_flag')},0)),\"нет\")",
                         font=S.FONT_NOTE, fill=S.FILL_TILE, align=S.AL_CENTER,
                         fmt='"первый разрыв: "mmmm yyyy;;;"первый разрыв: "@')
                self.name("Первый_разрыв", sh, absr(col, row + 2))
            ws.row_dimensions[row + 1].height = 30
        for nm, (row, col) in (("Макс_разрыв", tiles[5]), ("ЧП_итого", tiles[1]),
                               ("ROI_период", tiles[3])):
            cell = f"{CL(col)}{row + 1}"
            op = "greaterThan" if nm == "Макс_разрыв" else "lessThan"
            ws.conditional_formatting.add(cell, CellIsRule(
                operator=op, formula=["0"], font=Font(color=S.C_RED, bold=True, size=18)))
        # статус
        r = 13
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=13)
        self.put(sh, r, 2,
                 '=IF(Макс_разрыв>0,"⚠ Кассовый разрыв: в худший месяц не хватает "'
                 '&FIXED(Макс_разрыв,0)&" ₽. Нужен доп. капитал, меньшие партии или отсрочка '
                 'оплаты поставщику.","✓ Денег хватает на весь период: кассового разрыва нет.")',
                 font=S.FONT_WARN, align=S.AL_LEFT)
        ws.conditional_formatting.add(f"B{r}", FormulaRule(
            formula=["Макс_разрыв=0"], font=Font(color=S.C_GREEN, bold=True, size=11)))
        profit_rng = ref(SH_UNIT, f"$C${U['profit']}:${CL(2 + K)}${U['profit']}")
        r = 14
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=13)
        self.put(sh, r, 2,
                 f'=IF(COUNTIF({profit_rng},"<0")>0,"⚠ Есть товары с убытком на каждой продаже — '
                 f'см. таблицу ниже. ","")'
                 f'&IF(Упущено_шт>0,"⚠ Упущено продаж из-за нехватки товара: "&FIXED(Упущено_шт,0)'
                 f'&" шт. ","")'
                 f'&IF(AND(Налог_номер<3,Выручка_макс_год>Порог_НДС_УСН),"⚠ Выручка за '
                 f'календарный год выше порога НДС для УСН — уточните НДС у бухгалтера. ","")'
                 f'&IF(Налог_номер=3,"⚠ НПД не применяется при перепродаже товаров (422-ФЗ, ст. 4)'
                 f' — режим только для сравнения. ","")'
                 f'&IF(AND(Налог_номер=3,Выручка_макс_год>Лимит_НПД),"⚠ Доход выше лимита НПД. ",'
                 f'"")',
                 font=Font(name=S.FONT_NAME, size=10, bold=True, color="C55A11"), align=S.AL_LEFT)
        self.put(sh, 15, 2, '="Контроль баланса модели: "&Контроль_баланса&"   •   '
                            'остаток денег на конец: "&FIXED(Деньги_конец,0)&" ₽"',
                 font=S.FONT_NOTE)
        # таблица товаров
        r = 17
        heads = ["Товар", "", "Цена, ₽", "Себест. на складе МП, ₽", "Прибыль на 1 шт, ₽",
                 "Маржа", "ROI на 1 шт", "Точка безубыт., шт/мес", "План на плато, шт/мес",
                 "Продано за период, шт", "Выручка за период, ₽", "Упущено, шт"]
        self.header_row(sh, r, heads, c1=2)
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=3)
        ws.row_dimensions[r].height = 42
        self.L.dash_sku_row0 = r + 1
        FR = self.L.flow_rows
        for k in range(K):
            rr = r + 1 + k
            col = CL(3 + k)
            self.put(sh, rr, 2, f"={ref(SH_UNIT, f'{col}${self.unit_names_row}')}",
                     font=S.FONT_BOLD, border=S.BORDER)
            self.put(sh, rr, 3, None, border=S.BORDER)
            ws.merge_cells(start_row=rr, start_column=2, end_row=rr, end_column=3)
            vals = [
                (f"={self.unit_ref('price', k)}", S.F_RUB),
                (f"={self.unit_ref('landed', k)}", S.F_RUB),
                (f"={self.unit_ref('profit', k)}", S.F_RUB),
                (f"={self.unit_ref('margin', k)}", S.F_PCT),
                (f"={self.unit_ref('roi', k)}", S.F_PCT),
                (f"={self.unit_ref('bep', k)}", S.F_QTY),
                (f"={self.unit_ref('plateau_eff', k)}", S.F_QTY),
                (f"={ref(SH_FLOW, f'D{FR.sales[k]}')}", S.F_QTY),
                (f"={ref(SH_PNL, f'D{self.L.pnl_sku_rows.revenue[k]}')}", S.F_RUB),
                (f"={ref(SH_FLOW, f'D{FR.lost[k]}')}", S.F_QTY),
            ]
            for j, (f, fmt) in enumerate(vals):
                self.put(sh, rr, 4 + j, f, fmt=fmt, border=S.BORDER, align=S.AL_RIGHT)
        rt = r + 1 + K
        self.put(sh, rt, 2, "Итого", font=S.FONT_BOLD, fill=S.FILL_TOTAL, border=S.BORDER)
        self.put(sh, rt, 3, None, fill=S.FILL_TOTAL, border=S.BORDER)
        ws.merge_cells(start_row=rt, start_column=2, end_row=rt, end_column=3)
        for j in range(10):
            col = 4 + j
            f = None
            if j in (7, 8, 9):
                f = f"=SUM({CL(col)}{r + 1}:{CL(col)}{rt - 1})"
            fmt = S.F_RUB if j == 8 else S.F_QTY
            self.put(sh, rt, col, f, font=S.FONT_BOLD, fill=S.FILL_TOTAL, fmt=fmt,
                     border=S.BORDER, align=S.AL_RIGHT)
        for j in (2, 3, 4):   # прибыль, маржа, ROI
            col = CL(4 + j)
            ws.conditional_formatting.add(f"{col}{r + 1}:{col}{rt - 1}", CellIsRule(
                operator="lessThan", formula=["0"], fill=S.FILL_RED,
                font=Font(color=S.C_RED, bold=True)))
        col = CL(4 + 5)
        ws.conditional_formatting.add(f"{col}{r + 1}:{col}{rt - 1}", FormulaRule(
            formula=[f"ISTEXT({col}{r + 1})"], fill=S.FILL_RED, font=Font(color=S.C_RED)))
        # служебные имена для статуса
        self.name("Упущено_шт", SH_FLOW, f"$D${FT['lost']}")
        self.name("Деньги_конец", SH_CASH, "$D$" + self.L.end_cells["cash"][1:])

        # графики
        chart_row = rt + 2
        for col, text in ((2, "Остаток денег на счёте, ₽"),
                          (6, "Выручка и чистая прибыль по месяцам, ₽"),
                          (10, "Из чего складывается цена, ₽ на 1 шт")):
            self.put(sh, chart_row, col, text, font=S.FONT_SUBTITLE)
        anchor_row = chart_row + 1
        wsc = self.ws[SH_CASH]
        wsp = self.ws[SH_PNL]
        wsu = self.ws[SH_UNIT]
        cats = Reference(wsc, min_col=mcol(1), max_col=mcol(HORIZON), min_row=ROW_DATE)

        def style_chart(ch, title):
            ch.title = None
            ch.height = 7.6
            ch.width = 10.2
            ch.legend.position = "b"
            ch.legend.overlay = False
            for s_ in ch.series:
                s_.invertIfNegative = False
            ch.x_axis.delete = False
            ch.y_axis.delete = False
            ch.y_axis.number_format = '#,##0'
            ch.x_axis.number_format = 'mmm yy'
            ch.x_axis.tickLblPos = "low"

        # 1. остаток денег
        ch1 = BarChart()
        ch1.type = "col"
        ch1.grouping = "stacked"
        ch1.overlap = 100
        ch1.gapWidth = 40
        for key, title, color in (("cash_pos", "Остаток денег", "2F5597"),
                                  ("cash_neg", "Кассовый разрыв", "C00000")):
            s = Series(Reference(wsc, min_col=mcol(1), max_col=mcol(HORIZON), min_row=C[key]),
                       title=title)
            s.graphicalProperties.solidFill = color
            s.graphicalProperties.line.solidFill = color
            ch1.series.append(s)
        ch1.set_categories(cats)
        style_chart(ch1, "Остаток денег на счёте, ₽")
        ws.add_chart(ch1, f"B{anchor_row}")
        # 2. выручка и чистая прибыль
        ch2 = BarChart()
        ch2.type = "col"
        ch2.grouping = "clustered"
        ch2.gapWidth = 60
        for key, title, color in (("revenue", "Выручка", "8EA9DB"),
                                  ("net", "Чистая прибыль", "548235")):
            s = Series(Reference(wsp, min_col=mcol(1), max_col=mcol(HORIZON), min_row=P[key]),
                       title=title)
            s.graphicalProperties.solidFill = color
            s.graphicalProperties.line.solidFill = color
            ch2.series.append(s)
        ch2.set_categories(Reference(wsp, min_col=mcol(1), max_col=mcol(HORIZON),
                                     min_row=ROW_DATE))
        style_chart(ch2, "Выручка и чистая прибыль по месяцам, ₽")
        ws.add_chart(ch2, f"F{anchor_row}")
        # 3. структура цены
        ch3 = BarChart()
        ch3.type = "bar"
        ch3.grouping = "stacked"
        ch3.overlap = 100
        ch3.gapWidth = 50
        parts = [("landed", "Себестоимость", "8497B0"),
                 ("commission", "Комиссия", "F4B183"),
                 ("mp_logistics", "Логистика", "FFD966"),
                 ("ads", "Реклама", "C9C9C9"),
                 ("tax", "Налог", "A9D18E"),
                 ("profit", "Прибыль", "548235")]
        for key, title, color in parts:
            s = Series(Reference(wsu, min_col=3, max_col=2 + K, min_row=U[key]), title=title)
            s.graphicalProperties.solidFill = color
            s.graphicalProperties.line.solidFill = color
            ch3.series.append(s)
        ch3.set_categories(Reference(wsu, min_col=3, max_col=2 + K, min_row=self.unit_names_row))
        style_chart(ch3, "Из чего складывается цена, ₽ на 1 шт")
        ch3.x_axis.number_format = "General"
        ch3.y_axis.number_format = '#,##0'
        ch3.x_axis.tickLblPos = "low"
        ch3.x_axis.scaling.orientation = "maxMin"
        ch3.y_axis.crosses = "max"
        ws.add_chart(ch3, f"J{anchor_row}")
        self.L.charts = 3

        # печать / PDF: одна страница A4 альбомная
        last_row = anchor_row + 15
        ws.print_area = f"A1:M{last_row}"
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 1
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        ws.page_margins.left = ws.page_margins.right = 0.4
        ws.page_margins.top = ws.page_margins.bottom = 0.4
        ws.print_options.horizontalCentered = True
        ws.oddFooter.left.text = "Финансовая модель импорта из Китая • демо"
        ws.oddFooter.right.text = "Стр. &P из &N"

    # ================================================================ Инструкция
    def build_info(self):
        sh = SH_INFO
        ws = self.ws[sh]
        a = self.a
        ws.column_dimensions["A"].width = 3
        ws.column_dimensions["B"].width = 26
        ws.column_dimensions["C"].width = 100
        self.put(sh, 1, 2, a.title, font=S.FONT_TITLE)
        self.put(sh, 2, 2, a.demo_note, font=S.FONT_DEMO)
        r = 4

        def head(text):
            nonlocal r
            self.put(sh, r, 2, text, font=S.FONT_SUBTITLE, border=S.BORDER_BOTTOM)
            self.put(sh, r, 3, None, border=S.BORDER_BOTTOM)
            r += 1

        def line(text, label=None, font=S.FONT):
            nonlocal r
            if label is not None:
                self.put(sh, r, 2, label, font=S.FONT_BOLD, align=S.AL_WRAP)
            self.put(sh, r, 3, text, font=font, align=S.AL_WRAP)
            r += 1

        head("Что считает модель")
        for t in [
            "Закупку товара в Китае партиями, доставку, таможенные платежи и продажи на "
            "маркетплейсе на 18 месяцев вперёд.",
            "Юнит-экономику каждого товара: полную себестоимость, прибыль, маржу, наценку, ROI и "
            "точку безубыточности.",
            "Денежный поток: когда уходят деньги на закупку и когда приходят выплаты маркетплейса; "
            "показывает кассовый разрыв — месяцы, когда денег не хватает.",
            "ОПиУ (прибыль и убытки) по месяцам с налогом по выбранному режиму.",
            "Три сценария (базовый, пессимистичный, оптимистичный) и таблицы чувствительности "
            "к курсу юаня, цене и объёму продаж.",
        ]:
            line("• " + t)
        r += 1
        head("Как пользоваться")
        steps = [
            ("1. Допущения", "Впишите свои данные в жёлтые ячейки: курс, тарифы доставки, "
                             "товары (закупка в юанях, вес, объём, партия, цена продажи, плато "
                             "продаж), ставки пошлины и НДС, комиссию и логистику маркетплейса, "
                             "налоговый режим, стартовый капитал."),
            ("2. Темп продаж", "На листе «Допущения», раздел 4: сколько процентов от плато "
                               "продаётся в каждом месяце. Первые месяцы товар едет — ставьте 0 %."),
            ("3. Сценарии", "Выберите сценарий в жёлтой ячейке листа «Сценарии». Проценты "
                            "отклонений тоже можно менять."),
            ("4. Дашборд", "Главные цифры, предупреждения и графики. Красное — внимание: "
                           "кассовый разрыв, убыточный товар."),
            ("5. Детали", "«Юнит-экономика» — каждая статья затрат на 1 шт; «Закупки и склад» — "
                          "когда и сколько заказывать; «Денежный поток» и «ОПиУ» — по месяцам."),
            ("6. Чувствительность", "Сколько заработаете при другом курсе, цене или объёме — "
                                    "без ручного перебора."),
        ]
        for label, text in steps:
            line(text, label)
        r += 1
        head("Цвета ячеек")
        legend = [
            ("Ввод данных", "Жёлтая ячейка с синим шрифтом — меняйте. Только эти ячейки не "
                            "защищены.", S.FILL_INPUT, S.FONT_INPUT),
            ("Расчёт", "Белая ячейка — формула. Защищена от случайной правки.", None, S.FONT),
            ("Итог", "Серая ячейка с жирным шрифтом — итоговая строка.", S.FILL_TOTAL,
             S.FONT_BOLD),
            ("Служебная", "Серый курсив — техническая ячейка модели, не меняйте.", S.FILL_SERVICE,
             S.FONT_SERVICE),
            ("Внимание", "Красная заливка — кассовый разрыв, убыток, нехватка товара.", S.FILL_RED,
             Font(name=S.FONT_NAME, size=10, bold=True, color=S.C_RED)),
            ("Заказ", "Зелёная заливка на листе «Закупки и склад» — месяц заказа партии.",
             S.FILL_GREEN, Font(name=S.FONT_NAME, size=10, bold=True, color=S.C_GREEN)),
        ]
        for label, text, fill, font in legend:
            self.put(sh, r, 2, label, font=font, fill=fill, border=S.BORDER, align=S.AL_CENTER)
            self.put(sh, r, 3, text, align=S.AL_WRAP)
            r += 1
        r += 1
        head("Листы книги (нажмите, чтобы перейти)")
        sheets = [
            (SH_DASH, "главные показатели, предупреждения, графики; печатается на одну страницу"),
            (SH_IN, "все исходные данные — только здесь и на листе «Сценарии»"),
            (SH_SC, "выбор сценария и сравнение трёх сценариев в одной таблице"),
            (SH_UNIT, "расчёт на 1 проданную единицу по каждому товару"),
            (SH_FLOW, "спрос, продажи, остатки, товар в пути, заказы партиями"),
            (SH_CASH, "поступления и выплаты по месяцам, остаток денег, кассовый разрыв, "
                      "контроль баланса"),
            (SH_PNL, "выручка, себестоимость, расходы, налог, чистая прибыль по месяцам"),
            (SH_SENS, "таблицы «что если»: курс × цена, курс × объём"),
        ]
        for name, text in sheets:
            c = self.put(sh, r, 2, name, font=S.FONT_LINK)
            c.hyperlink = f"#'{name}'!A1"
            self.put(sh, r, 3, text)
            r += 1
        r += 1
        head("Допущения и упрощения")
        for t in [
            "Курс юаня постоянный внутри сценария; колебания курса — через сценарии и лист "
            "«Чувствительность».",
            "Таможенная стоимость = товар + доставка до склада в РФ (консервативно). Пошлина — от "
            "таможенной стоимости, НДС на ввоз — от таможенной стоимости с пошлиной. НДС на ввоз "
            "включён в себестоимость: на УСН и НПД он к вычету не принимается.",
            "Ставки пошлин, НДС, налогов, тарифы доставки и маркетплейса в книге — ПРИМЕР. Задайте "
            "свои: пошлина — по коду ТН ВЭД, комиссия — по категории товара на своей площадке.",
            "Продажи = выкупы. Возвраты увеличивают логистику маркетплейса (доставка туда и "
            "обратно), возвращённый товар снова идёт в продажу.",
            "Заказ поставщику: товар оплачивается в месяц заказа, доставка и таможня — в месяц "
            "поступления. Заказ кратен партии; продажи не превышают остаток на складе.",
            "Налог: база — данные ОПиУ по месяцу продажи (упрощение кассового метода УСН). УСН "
            "платится поквартально в месяце после квартала, НПД — в следующем месяце. Взносы ИП и "
            "НДС на УСН при превышении порога не считаются — дашборд предупреждает о пороге.",
            "Окупаемость — месяц, начиная с которого накопленный денежный поток больше не уходит "
            "в минус. ROI = чистая прибыль / пик вложенных денег.",
        ]:
            line("• " + t)
        r += 1
        head("Защита и проверка")
        line("Листы защищены без пароля, чтобы случайно не стереть формулу. Снять защиту: "
             "Рецензирование → Снять защиту листа.")
        line("Каждая формула сверена с независимым расчётом на Python (зеркальная модель): "
             "расхождение не больше 0,01 ₽. На листе «Денежный поток» есть контроль баланса: "
             "деньги + товар + дебиторка − налог = капитал + прибыль.")
        line("Лист «Чувствительность» и сравнение сценариев используют «Таблицу данных» Excel "
             "(Данные → Анализ «что если»). В Google Таблицах эти две таблицы не пересчитываются.")

    # ================================================================ сборка
    def build(self):
        self.build_inputs()
        self.build_scenarios()
        self.build_unit()
        self.plan_pnl_cash_rows()
        self.build_flow()
        self.build_pnl()
        self.build_cash()
        self.build_sensitivity()
        self.build_dashboard()
        self.build_info()
        # KPI для зеркала
        self.L.kpi_names.update({
            "revenue": "Выручка_итого", "net_profit": "ЧП_итого", "net_margin": "Рентабельность",
            "roi": "ROI_период", "peak_investment": "Пик_вложений", "max_gap": "Макс_разрыв",
            "payback": "Окупаемость_мес", "end_equity": "Капитал_конец",
            "first_gap": "Первый_разрыв", "lost_units": "Упущено_шт", "end_cash": "Деньги_конец",
            "max_annual_revenue": "Выручка_макс_год",
        })
        # защита листов (без пароля) и цвета ярлыков
        tab = {SH_INFO: "7F7F7F", SH_DASH: "548235", SH_IN: "FFC000", SH_SC: "FFC000",
               SH_SENS: "2F5597"}
        for name, ws in self.ws.items():
            ws.protection.sheet = True
            ws.protection.formatColumns = False
            ws.protection.formatRows = False
            ws.protection.selectLockedCells = False
            ws.protection.selectUnlockedCells = False
            ws.sheet_properties.tabColor = tab.get(name, "8EA9DB")
            if name not in (SH_DASH,):
                ws.page_setup.orientation = "landscape"
                ws.page_setup.paperSize = ws.PAPERSIZE_A4
                ws.page_setup.fitToWidth = 1
                ws.page_setup.fitToHeight = 0
                ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        self.wb.active = SHEET_ORDER.index(SH_DASH)
        for ws in self.ws.values():
            ws.sheet_view.tabSelected = ws.title == SH_DASH
        self.wb.calculation.fullCalcOnLoad = True
        return self.wb, self.L


def build_workbook(a: Assumptions):
    """Собрать книгу. Возвращает (Workbook, Layout)."""
    return Builder(a).build()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Генератор финансовой модели в Excel")
    ap.add_argument("--config", help="JSON с допущениями (по умолчанию — демо)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "examples" /
                                         "finmodel-import-china.xlsx"))
    args = ap.parse_args(argv)
    from .config import load_config
    a = load_config(args.config) if args.config else demo_config()
    wb, _ = build_workbook(a)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(f"Книга сохранена: {out}")
    print("Внимание: значения формул появятся после открытия в Excel "
          "(или запустите python -m finmodel.excel_check для пересчёта, сверки и PDF).")


if __name__ == "__main__":
    main()
