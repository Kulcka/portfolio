"""merge: объединение многих Excel/CSV/JSON в одну таблицу.

- колонки сопоставляются по названиям через словарь синонимов (columns.yaml)
  и нечёткое сравнение для опечаток;
- у каждой строки — источник (файл/лист) и номер строки в нём;
- отчёт: какие колонки как сопоставлены, чего не хватает в каждом файле,
  какие колонки не узнаны (добавлены в конец как есть).
"""

from __future__ import annotations

import glob
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import fuzz

from . import DocToolsError
from .normalize import is_empty, norm_key
from .report import Report
from .tableio import TABLE_EXT, Sheet, Table, read_tables, write_xlsx

DEFAULT_CONFIG = Path(__file__).with_name("columns.yaml")
SOURCE_COL = "Источник"
SOURCE_ROW_COL = "Строка источника"


@dataclass
class ColumnConfig:
    columns: dict[str, list[str]]
    threshold: int = 88

    @classmethod
    def load(cls, path: Path | None = None) -> "ColumnConfig":
        path = Path(path) if path else DEFAULT_CONFIG
        if not path.exists():
            raise DocToolsError(f"Не найден файл словаря колонок: {path}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise DocToolsError(f"{path.name}: ошибка в YAML ({exc})") from exc
        cols = data.get("columns")
        if not isinstance(cols, dict) or not cols:
            raise DocToolsError(f"{path.name}: нужен раздел columns: {{Название: [синонимы]}}")
        return cls({str(k): [str(s) for s in (v or [])] for k, v in cols.items()}, int(data.get("fuzzy_threshold", 88)))

    def match(self, name: str) -> tuple[str | None, str]:
        """(каноническое название или None, как сопоставлено)."""
        key = norm_key(name)
        for canon, syns in self.columns.items():
            if key == norm_key(canon) or key in {norm_key(s) for s in syns}:
                return canon, "по словарю"
        best: tuple[float, str, str] = (0.0, "", "")
        for canon, syns in self.columns.items():
            for s in [canon, *syns]:
                score = fuzz.ratio(key, norm_key(s))
                if score > best[0]:
                    best = (score, canon, s)
        if best[0] >= self.threshold:
            return best[1], f"похоже на «{best[2]}» ({best[0]:.0f}%)"
        return None, "нет в словаре"


def expand_inputs(items: list[str | Path], exts: set[str] = TABLE_EXT) -> list[Path]:
    """Файлы, папки и маски (*.xlsx) -> список файлов в порядке указания, без повторов."""
    out: list[Path] = []
    for item in items:
        s = str(item)
        if any(ch in s for ch in "*?["):
            matches = [Path(m) for m in sorted(glob.glob(s))]
            if not matches:
                raise DocToolsError(f"По маске «{s}» файлов не найдено")
        elif Path(s).is_dir():
            matches = sorted(p for p in Path(s).iterdir() if p.suffix.lower() in exts and not p.name.startswith("~$"))
        else:
            matches = [Path(s)]
        for m in matches:
            if not m.exists():
                raise DocToolsError(f"Файл не найден: {m}")
            if m not in out:
                out.append(m)
    if not out:
        raise DocToolsError("Не передано ни одного файла")
    return out


def merge_files(inputs: list[str | Path], dst: Path, config: Path | None = None,
                sheet: str | None = None) -> tuple[Path, Report]:
    cfg = ColumnConfig.load(config)
    files = expand_inputs(inputs)
    rep = Report(f"Объединение таблиц: {len(files)} файл(ов)")
    tables: list[Table] = []
    for f in files:
        found = read_tables(f, sheet)
        if not found:
            rep.warn(f"{f.name}: нет данных — пропущен")
        tables += found

    mapping_rows: list[list[Any]] = []
    plans: list[tuple[Table, list[str]]] = []
    present: set[str] = set()
    extras: list[str] = []
    fuzzy_used = 0
    for t in tables:
        targets: list[str] = []
        used: set[str] = set()
        for col in t.columns:
            canon, how = cfg.match(col)
            if canon and canon in used:
                rep.warn(f"{t.name}: колонки «{col}» и ещё одна сопоставились с «{canon}» — «{col}» оставлена отдельно")
                canon, how = None, "дубль сопоставления"
            if canon:
                used.add(canon)
                present.add(canon)
                target = canon
                fuzzy_used += how.startswith("похоже")
            else:
                target = col
                if col not in extras:
                    extras.append(col)
            targets.append(target)
            mapping_rows.append([t.name, col, target, how])
        plans.append((t, targets))

    out_cols = [c for c in cfg.columns if c in present] + [c for c in extras if c not in cfg.columns] + [SOURCE_COL, SOURCE_ROW_COL]
    index = {c: i for i, c in enumerate(out_cols)}
    rows: list[list[Any]] = []
    per_file: list[list[Any]] = []
    for t, targets in plans:
        kept = skipped = 0
        for row, src_row in zip(t.rows, t.row_numbers):
            if all(is_empty(v) for v in row):
                skipped += 1
                continue
            out = [None] * len(out_cols)
            for v, target in zip(row, targets):
                out[index[target]] = v
            out[index[SOURCE_COL]] = t.name
            out[index[SOURCE_ROW_COL]] = src_row
            rows.append(out)
            kept += 1
        missing = [c for c in cfg.columns if c in present and c not in targets]
        unknown = [c for c, tg in zip(t.columns, targets) if tg not in cfg.columns]
        per_file.append([t.name, kept, skipped, ", ".join(missing) or "—", ", ".join(unknown) or "—"])
        if missing:
            rep.warn(f"{t.name}: нет колонок {', '.join(missing)} — в объединённой таблице они пустые для строк этого файла")
        if skipped:
            rep.info(f"{t.name}: пропущено пустых строк — {skipped}")

    if extras:
        rep.warn(f"Колонки не из словаря (добавлены в конец как есть): {', '.join(extras)}. "
                 "Если это синонимы — допишите их в словарь и запустите снова")
    if fuzzy_used:
        rep.warn(f"Нечётких сопоставлений: {fuzzy_used} — проверьте лист «Сопоставление колонок»")
    rep.stat("Файлов/листов", len(tables))
    rep.stat("Строк в итоге", len(rows))
    rep.stat("Колонок в итоге", len(out_cols))
    rep.table("По файлам", ["Файл / лист", "Строк взято", "Пустых пропущено", "Нет колонок", "Не из словаря"], per_file)
    rep.table("Сопоставление колонок", ["Файл / лист", "Колонка в файле", "Колонка в итоге", "Как сопоставлено"], mapping_rows)

    write_xlsx(dst, [
        Sheet("Объединено", out_cols, rows),
        Sheet("Сопоставление колонок", ["Файл / лист", "Колонка в файле", "Колонка в итоге", "Как сопоставлено"], mapping_rows),
        Sheet("Отчёт", [], rep.sheet_rows(), plain=True),
    ])
    rep.output(Path(dst))
    return Path(dst), rep
