"""Отчёт о выполненной обработке: что сделано, что не распознано, на что обратить внимание.

Каждая команда наполняет один объект Report; он печатается в консоль кратко,
сохраняется рядом с результатом в Markdown и (для Excel-результатов) кладётся
отдельным листом «Отчёт».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

INFO = "info"
WARN = "warn"
ERROR = "error"

_MARK = {INFO: "", WARN: "⚠ ", ERROR: "✖ "}


@dataclass
class ReportTable:
    title: str
    headers: list[str]
    rows: list[list[Any]]
    limit: int = 200  # в Markdown показываем не больше, полный список — в Excel


@dataclass
class Report:
    title: str
    stats: dict[str, Any] = field(default_factory=dict)
    items: list[tuple[str, str]] = field(default_factory=list)
    tables: list[ReportTable] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)
    created: datetime = field(default_factory=datetime.now)

    # --- наполнение -------------------------------------------------------
    def stat(self, key: str, value: Any) -> None:
        self.stats[key] = value

    def info(self, text: str) -> None:
        self.items.append((INFO, text))

    def warn(self, text: str) -> None:
        self.items.append((WARN, text))

    def error(self, text: str) -> None:
        self.items.append((ERROR, text))

    def table(self, title: str, headers: list[str], rows: list[list[Any]], limit: int = 200) -> None:
        if rows:
            self.tables.append(ReportTable(title, headers, rows, limit))

    def output(self, path: Path) -> None:
        self.outputs.append(Path(path))

    @property
    def warnings(self) -> list[str]:
        return [t for lvl, t in self.items if lvl in (WARN, ERROR)]

    # --- вывод ------------------------------------------------------------
    def to_markdown(self) -> str:
        lines = [f"# {self.title}", "", f"Дата обработки: {self.created:%d.%m.%Y %H:%M}", ""]
        if self.stats:
            lines += ["## Итоги", ""]
            lines += [f"- **{k}:** {_fmt(v)}" for k, v in self.stats.items()]
            lines.append("")
        if self.outputs:
            lines += ["## Результаты", ""]
            lines += [f"- `{p.name}`" for p in self.outputs]
            lines.append("")
        if self.items:
            lines += ["## Что сделано и на что обратить внимание", ""]
            lines += [f"- {_MARK[lvl]}{text}" for lvl, text in self.items]
            lines.append("")
        for t in self.tables:
            lines += [f"## {t.title}", ""]
            lines.append("| " + " | ".join(t.headers) + " |")
            lines.append("|" + "---|" * len(t.headers))
            for row in t.rows[: t.limit]:
                lines.append("| " + " | ".join(_md_cell(v) for v in row) + " |")
            if len(t.rows) > t.limit:
                lines.append(f"\n…и ещё {len(t.rows) - t.limit} строк (полный список — в файле результата).")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_markdown(), encoding="utf-8")
        return path

    def sheet_rows(self) -> list[list[Any]]:
        """Отчёт в виде строк для листа Excel «Отчёт»."""
        rows: list[list[Any]] = [[self.title], [f"Дата обработки: {self.created:%d.%m.%Y %H:%M}"], []]
        for k, v in self.stats.items():
            rows.append([k, _fmt(v)])
        if self.items:
            rows += [[], ["Что сделано и на что обратить внимание"]]
            rows += [[_MARK[lvl] + text] for lvl, text in self.items]
        for t in self.tables:
            rows += [[], [t.title], list(t.headers)]
            rows += [list(r) for r in t.rows]
        return rows

    def console_summary(self) -> str:
        lines = [self.title]
        lines += [f"  {k}: {_fmt(v)}" for k, v in self.stats.items()]
        warns = self.warnings
        if warns:
            lines.append(f"  Предупреждений: {len(warns)}")
            lines += [f"    - {w}" for w in warns[:8]]
            if len(warns) > 8:
                lines.append(f"    …ещё {len(warns) - 8}, см. отчёт")
        for p in self.outputs:
            lines.append(f"  -> {p}")
        return "\n".join(lines)


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}".replace(",", " ").replace(".", ",")
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    return str(v)


def _md_cell(v: Any) -> str:
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%d.%m.%Y")
    return str(v).replace("|", "\\|").replace("\n", " ")
