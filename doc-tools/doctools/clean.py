"""clean: чистка таблицы с листами «до/после» и журналом изменений.

Шаги:
1. лишние и неразрывные пробелы во всех текстовых ячейках;
2. приведение по типу колонки (тип определяется по названию, можно задать вручную):
   телефон -> +7 (900) 000-12-34; ИНН -> цифры (+ восстановление ведущих нулей,
   проверка контрольных цифр); e-mail -> нижний регистр; даты -> даты Excel;
   числа с любыми разделителями -> числа; ФИО/город -> «Как Положено»;
3. удаление пустых строк;
4. точные дубли (по всем колонкам или по ключевым) — лишние удаляются,
   пустые ячейки оставленной строки дополняются из дубля, расхождения — в отчёт;
5. нечёткие дубли (опечатки, неполное ФИО) — только помечаются, удаляются по флагу.

Ничего не проверяется по внешним базам.
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process

from . import DocToolsError
from .merge import SOURCE_COL, SOURCE_ROW_COL
from .normalize import (clean_spaces, is_empty, norm_key, normalize_email, normalize_inn,
                        normalize_phone, parse_date, parse_number, title_name)
from .report import Report
from .tableio import (ADDED_FILL, CHANGED_FILL, PROBLEM_FILL, Sheet, Table, read_table, to_text,
                      write_csv, write_xlsx)

TYPES = {
    "phone": "телефон", "inn": "ИНН", "email": "e-mail", "date": "дата",
    "number": "число", "name": "имя/название", "text": "текст", "skip": "не трогать",
}
TYPE_ALIASES = {v: k for k, v in TYPES.items()} | {"телефон": "phone", "инн": "inn", "почта": "email",
                                                   "дата": "date", "число": "number", "фио": "name",
                                                   "имя": "name", "текст": "text", "пропустить": "skip"}
AUTO_RULES: list[tuple[tuple[str, ...], str]] = [
    (("телефон", "тел", "phone", "моб"), "phone"),
    (("инн", "inn"), "inn"),
    (("email", "e mail", "почта", "mail"), "email"),
    (("дата", "date", "срок"), "date"),
    (("сумма", "цена", "стоимость", "количество", "кол во", "остаток", "итого", "amount", "price", "qty"), "number"),
    (("фио", "имя", "фамилия", "контакт", "клиент", "город", "населенный пункт", "name"), "name"),
]
SERVICE_COLS = {SOURCE_COL, SOURCE_ROW_COL}


def detect_type(column: str) -> str:
    key = norm_key(column)
    words = key.split()
    for hints, kind in AUTO_RULES:
        for h in hints:
            if (" " in h and h in key) or h in words or any(w.startswith(h) for w in words if len(h) >= 4):
                return kind
    return "text"


def parse_type_overrides(items: list[str] | None, columns: list[str]) -> dict[str, str]:
    """«Колонка=тип» -> {колонка: тип}. Тип по-русски или по-английски."""
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise DocToolsError(f"Не понял «{item}». Нужно «Колонка=тип», например «Моб=телефон»")
        col, kind = (s.strip() for s in item.rsplit("=", 1))
        kind = TYPE_ALIASES.get(kind.lower(), kind.lower())
        if kind not in TYPES:
            raise DocToolsError(f"Неизвестный тип «{kind}». Варианты: {', '.join(TYPES.values())}")
        out[_find_col(col, columns)] = kind
    return out


def _find_col(name: str, columns: list[str]) -> str:
    for c in columns:
        if norm_key(c) == norm_key(name):
            return c
    raise DocToolsError(f"Нет колонки «{name}». Есть: {', '.join(columns)}")


@dataclass
class CleanResult:
    table: Table
    before: Table
    changes: list[list[Any]] = field(default_factory=list)      # строка в файле, колонка, было, стало, что сделано
    problems: list[list[Any]] = field(default_factory=list)     # строка в файле, колонка, значение, причина
    removed: list[list[Any]] = field(default_factory=list)      # строка в файле, причина, + значения
    fuzzy_groups: list[list[int]] = field(default_factory=list)  # индексы строк результата
    conflicts: list[list[Any]] = field(default_factory=list)
    fills: dict[tuple[int, int], Any] = field(default_factory=dict)
    rules: Counter = field(default_factory=Counter)
    types: dict[str, str] = field(default_factory=dict)


def _clean_cell(v: Any, kind: str) -> tuple[Any, list[str], str | None]:
    """(новое значение, что сделано, проблема)."""
    rules: list[str] = []
    if kind == "skip":
        return v, rules, None
    if isinstance(v, str):
        s = clean_spaces(v)
        if s != v:
            rules.append("пробелы")
        v = s or None
        if v is None:
            return None, rules, None
    if v is None:
        return None, rules, None
    if kind == "phone":
        p = normalize_phone(v)
        if p is None:
            return v, rules, "телефон не распознан"
        if p != v:
            rules.append("формат телефона")
        return p, rules, None
    if kind == "inn":
        n, note = normalize_inn(v)
        if n is None:
            return v, rules, note
        if n != v:
            rules.append("ИНН: " + (note or "только цифры"))
        return n, rules, (note if note == "контрольные цифры не сходятся" else None)
    if kind == "email":
        e = normalize_email(v)
        if e is None:
            return v, rules, "некорректный e-mail"
        if e != v:
            rules.append("e-mail в нижний регистр")
        return e, rules, None
    if kind == "date":
        d = parse_date(v)
        if d is None:
            return v, rules, "дата не распознана"
        if not isinstance(v, (date, datetime)):
            rules.append("текст -> дата")
        return d, rules, None
    if kind == "number":
        n = parse_number(v)
        if n is None:
            return v, rules, "число не распознано"
        if not isinstance(v, (int, float)):
            rules.append("текст -> число")
        return n, rules, None
    if kind == "name" and isinstance(v, str):
        t = title_name(v)
        if t != v:
            rules.append("регистр")
        return t, rules, None
    return v, rules, None


def _row_key(row: list[Any], idx: list[int]) -> tuple[str, ...]:
    return tuple(norm_key(to_text(row[i])) for i in idx)


def clean_table(table: Table, *, types: dict[str, str] | None = None, dedup_keys: list[str] | None = None,
                fuzzy_keys: list[str] | None = None, fuzzy_threshold: int = 90, drop_fuzzy: bool = False,
                keep_empty: bool = False) -> CleanResult:
    columns = table.columns
    kinds = {c: ("skip" if c in SERVICE_COLS else detect_type(c)) for c in columns}
    kinds.update(types or {})
    res = CleanResult(table=Table(list(columns), [], table.name), before=copy.deepcopy(table), types=kinds)
    data_idx = [i for i, c in enumerate(columns) if c not in SERVICE_COLS]

    # 1-2. значения
    rows: list[list[Any]] = []
    src: list[int] = []
    marks: list[dict[int, Any]] = []
    for row, rn in zip(table.rows, table.row_numbers):
        new_row, mark = [], {}
        for ci, v in enumerate(row):
            nv, rules, problem = _clean_cell(v, kinds[columns[ci]])
            if rules:
                res.changes.append([rn, columns[ci], v, nv, "; ".join(rules)])
                res.rules.update(rules)
                mark[ci] = CHANGED_FILL
            if problem:
                res.problems.append([rn, columns[ci], v, problem])
                mark[ci] = PROBLEM_FILL
            new_row.append(nv)
        # 3. пустые строки
        if not keep_empty and all(is_empty(new_row[i]) for i in data_idx):
            res.removed.append([rn, "пустая строка", ""] + new_row)
            continue
        rows.append(new_row)
        src.append(rn)
        marks.append(mark)

    # 4. точные дубли
    key_idx = [columns.index(_find_col(k, columns)) for k in dedup_keys] if dedup_keys else data_idx
    first: dict[tuple[str, ...], int] = {}
    keep = [True] * len(rows)
    for i, row in enumerate(rows):
        key = _row_key(row, key_idx)
        if not any(key):
            continue
        if key not in first:
            first[key] = i
            continue
        j = first[key]
        keep[i] = False
        res.removed.append([src[i], "дубль", src[j]] + row)
        for ci in data_idx:
            a, b = rows[j][ci], row[ci]
            if is_empty(b):
                continue
            if is_empty(a):
                rows[j][ci] = b
                marks[j][ci] = ADDED_FILL
                res.changes.append([src[j], columns[ci], None, b, f"дополнено из дубля (строка {src[i]})"])
                res.rules["дополнено из дубля"] += 1
            elif norm_key(to_text(a)) != norm_key(to_text(b)):
                res.conflicts.append([src[j], src[i], columns[ci], a, b])
    rows = [r for r, k in zip(rows, keep) if k]
    marks = [m for m, k in zip(marks, keep) if k]
    src = [s for s, k in zip(src, keep) if k]

    # 5. нечёткие дубли
    if fuzzy_keys:
        f_idx = [columns.index(_find_col(k, columns)) for k in fuzzy_keys]
        keys = [" ".join(norm_key(to_text(r[i])) for i in f_idx).strip() for r in rows]
        parent = list(range(len(rows)))

        def root(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i, k in enumerate(keys):
            if not k:
                continue
            for _, score, j in process.extract(k, {j: keys[j] for j in range(i + 1, len(keys)) if keys[j]},
                                               scorer=fuzz.token_set_ratio, score_cutoff=fuzzy_threshold, limit=None):
                parent[root(j)] = root(i)
        groups: dict[int, list[int]] = {}
        for i in range(len(rows)):
            groups.setdefault(root(i), []).append(i)
        res.fuzzy_groups = [g for g in groups.values() if len(g) > 1]
        if drop_fuzzy and res.fuzzy_groups:
            drop = set()
            for g in res.fuzzy_groups:
                for i in g[1:]:
                    drop.add(i)
                    res.removed.append([src[i], "похожая строка (нечёткий дубль)", src[g[0]]] + rows[i])
            rows = [r for i, r in enumerate(rows) if i not in drop]
            marks = [m for i, m in enumerate(marks) if i not in drop]
            src = [s for i, s in enumerate(src) if i not in drop]
            res.fuzzy_groups = []

    res.table.rows = rows
    res.table.row_numbers = src
    res.fills = {(ri, ci): fill for ri, m in enumerate(marks) for ci, fill in m.items()}
    return res


def clean_file(src: Path, dst: Path | None = None, *, sheet: str | None = None, header_row: int | None = None,
               type_overrides: list[str] | None = None, dedup_keys: list[str] | None = None,
               fuzzy_keys: list[str] | None = None, fuzzy_threshold: int = 90, drop_fuzzy: bool = False,
               keep_empty: bool = False) -> tuple[Path, Report]:
    src = Path(src)
    dst = Path(dst) if dst else src.with_name(src.stem + "_чистый.xlsx")
    table = read_table(src, sheet, header_row)
    if not table.rows:
        raise DocToolsError(f"{src.name}: в таблице нет строк данных")
    overrides = parse_type_overrides(type_overrides, table.columns)
    res = clean_table(table, types=overrides, dedup_keys=dedup_keys, fuzzy_keys=fuzzy_keys,
                      fuzzy_threshold=fuzzy_threshold, drop_fuzzy=drop_fuzzy, keep_empty=keep_empty)

    rep = Report(f"Чистка таблицы: {table.name}")
    rep.stat("Строк было", len(table.rows))
    rep.stat("Строк стало", len(res.table.rows))
    rep.stat("Ячеек изменено", len(res.changes))
    n_empty = sum(1 for r in res.removed if r[1] == "пустая строка")
    n_dup = sum(1 for r in res.removed if r[1] == "дубль")
    rep.stat("Пустых строк удалено", n_empty)
    rep.stat("Точных дублей удалено", n_dup)
    rep.stat("Групп похожих строк (проверить)", len(res.fuzzy_groups))
    rep.stat("Значений не распознано (проверить)", len(res.problems))
    typed = [f"{c} — {TYPES[k]}" for c, k in res.types.items() if k not in ("text", "skip")]
    rep.info("Типы колонок: " + (", ".join(typed) or "не определены") + " (задать вручную: --type \"Колонка=тип\")")
    rep.info("Ключ дублей: " + (", ".join(dedup_keys) if dedup_keys else "все колонки (полностью одинаковые строки)"))
    for rule, n in res.rules.most_common():
        rep.info(f"{rule}: {n}")
    if res.problems:
        rep.warn(f"Не распознано значений: {len(res.problems)} — оставлены как были и подсвечены красным")
    if res.conflicts:
        rep.warn(f"В дублях расходятся значения: {len(res.conflicts)} — оставлено значение первой строки, список ниже")
    if res.fuzzy_groups:
        rep.warn(f"Похожие строки: {len(res.fuzzy_groups)} групп — не удалены, см. лист «Возможные дубли» "
                 "(удалить автоматически: --drop-fuzzy)")
    rep.table("Не распознано", ["Строка в файле", "Колонка", "Значение", "Причина"], res.problems)
    rep.table("Расхождения в дублях", ["Оставлена строка", "Удалена строка", "Колонка", "Оставлено", "Отброшено"], res.conflicts)

    cols = res.table.columns
    fuzzy_rows: list[list[Any]] = []
    for n, g in enumerate(res.fuzzy_groups, start=1):
        for i in g:
            fuzzy_rows.append([n, i + 2, res.table.row_numbers[i]] + res.table.rows[i])
    if dst.suffix.lower() == ".csv":
        write_csv(dst, res.table)
    else:
        write_xlsx(dst, [
            Sheet("Результат", cols, res.table.rows, res.fills),
            Sheet("До", res.before.columns, res.before.rows),
            Sheet("Изменения", ["Строка в файле", "Колонка", "Было", "Стало", "Что сделано"], res.changes),
            Sheet("Удалённые строки", ["Строка в файле", "Причина", "Дубль строки"] + cols, res.removed),
            Sheet("Возможные дубли", ["Группа", "Строка в результате", "Строка в файле"] + cols, fuzzy_rows),
            Sheet("Отчёт", [], rep.sheet_rows() + [[], ["Цвета на листе «Результат»:"],
                                                   ["жёлтый — значение приведено к единому виду"],
                                                   ["зелёный — дополнено из удалённого дубля"],
                                                   ["красный — не распознано, проверьте вручную"]], plain=True),
        ])
    rep.output(dst)
    return dst, rep
