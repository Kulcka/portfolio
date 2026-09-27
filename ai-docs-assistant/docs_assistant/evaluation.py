"""Оценка качества на наборе вопросов.

Метрики:

* **hit@k** — доля вопросов по теме, для которых нужный фрагмент (любой из
  допустимых источников) оказался в первых k результатах поиска;
* **правильные отказы** — доля вопросов не по теме, на которые бот ответил
  «в документах нет»;
* **ложные отказы** — доля вопросов по теме, на которые бот отказался. Это
  контроль: отказываться всегда — значит получить 100% правильных отказов.

Отказы считаются сквозным прогоном ``ask`` через выбранного провайдера
(по умолчанию офлайн-провайдер ``extractive``, без обращения к сети).
Режим ``--tune`` подбирает порог полноты совпадения на dev-части и печатает
результат на test-части отдельно — порог не подгоняется под те же вопросы,
по которым о нём судят.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from docs_assistant.assistant import DocsAssistant
from docs_assistant.chunking import Chunk
from docs_assistant.citations import source_label
from docs_assistant.config import Settings


@dataclass(frozen=True)
class EvalQuestion:
    q: str
    split: str
    expected: tuple[dict[str, object], ...] = ()


@dataclass
class EvalReport:
    k: int
    in_domain: int = 0
    hits_at_1: int = 0
    hits_at_k: int = 0
    false_refusals: int = 0
    off_topic: int = 0
    correct_refusals: int = 0
    misses: list[str] = dataclasses.field(default_factory=list)
    wrong_answers: list[str] = dataclasses.field(default_factory=list)
    refused_in_domain: list[str] = dataclasses.field(default_factory=list)

    def lines(self) -> list[str]:
        def pct(a: int, b: int) -> str:
            return f"{a}/{b} = {a / b:.0%}" if b else "—"

        out = [
            f"hit@1: {pct(self.hits_at_1, self.in_domain)}",
            f"hit@{self.k}: {pct(self.hits_at_k, self.in_domain)}",
            f"Правильные отказы на вопросы не по теме: {pct(self.correct_refusals, self.off_topic)}",
            f"Ложные отказы на вопросы по теме: {pct(self.false_refusals, self.in_domain)}",
        ]
        out += [f"  промах поиска: {q}" for q in self.misses]
        out += [f"  ответил не по теме: {q}" for q in self.wrong_answers]
        out += [f"  отказал по теме: {q}" for q in self.refused_in_domain]
        return out


def load_questions(path: Path) -> tuple[list[EvalQuestion], list[EvalQuestion]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    in_domain = [
        EvalQuestion(q=item["q"], split=item.get("split", "test"), expected=tuple(item["expected"]))
        for item in data["in_domain"]
    ]
    off_topic = [EvalQuestion(q=item["q"], split=item.get("split", "test")) for item in data["off_topic"]]
    return in_domain, off_topic


def matches(chunk: Chunk, expected: dict[str, object]) -> bool:
    if chunk.doc != expected["doc"]:
        return False
    if "page" in expected and chunk.page != expected["page"]:
        return False
    if "section" in expected:
        wanted = str(expected["section"]).lower()
        if wanted not in (chunk.section or "").lower():
            return False
    return True


def evaluate(
    assistant: DocsAssistant,
    in_domain: Sequence[EvalQuestion],
    off_topic: Sequence[EvalQuestion],
    *,
    k: int = 3,
    full_pipeline: bool = True,
) -> EvalReport:
    report = EvalReport(k=k, in_domain=len(in_domain), off_topic=len(off_topic))
    for item in in_domain:
        result = assistant.search(item.q, k=max(k, assistant.settings.top_k))
        ranks = [
            rank
            for rank, hit in enumerate(result.hits[:k])
            if any(matches(hit.chunk, exp) for exp in item.expected)
        ]
        if ranks:
            report.hits_at_k += 1
            report.hits_at_1 += ranks[0] == 0
        else:
            top = result.hits[0].chunk if result.hits else None
            where = source_label(top.doc, top.page, top.section) if top else "ничего"
            report.misses.append(f"{item.q} → первым найдено {where}")
        if full_pipeline:
            answer = assistant.ask(item.q)
            refused, reason = answer.status == "no_answer", answer.reason
        else:
            refused, reason = not result.relevant, result.reason
        if refused:
            report.false_refusals += 1
            report.refused_in_domain.append(f"{item.q} ({reason})")
    for item in off_topic:
        if full_pipeline:
            refused = assistant.ask(item.q).status == "no_answer"
        else:
            refused = not assistant.search(item.q).relevant
        if refused:
            report.correct_refusals += 1
        else:
            report.wrong_answers.append(item.q)
    return report


def tune_threshold(
    assistant: DocsAssistant, in_domain: Sequence[EvalQuestion], off_topic: Sequence[EvalQuestion]
) -> tuple[float, float]:
    """Порог полноты совпадения, лучше всего разделяющий вопросы по теме и не по теме.

    Критерий — сбалансированная точность решения «отвечать/отказать»; при равенстве
    берётся середина диапазона лучших порогов (дальше от обеих ошибок).
    """
    retriever = assistant.retriever
    pos = [retriever.search(q.q, k=1).best_coverage for q in in_domain]
    neg = [retriever.search(q.q, k=1).best_coverage for q in off_topic]
    candidates = [round(0.05 * i, 2) for i in range(2, 19)]
    scored = []
    for threshold in candidates:
        tpr = sum(c >= threshold for c in pos) / len(pos) if pos else 0.0
        tnr = sum(c < threshold for c in neg) / len(neg) if neg else 0.0
        scored.append(((tpr + tnr) / 2, threshold))
    best_score = max(score for score, _ in scored)
    best = [threshold for score, threshold in scored if score == best_score]
    return best[len(best) // 2], best_score


def run_eval_cli(settings: Settings, questions_path: Path, *, k: int = 3, tune: bool = False) -> int:
    in_domain, off_topic = load_questions(questions_path)
    assistant = DocsAssistant.from_settings(settings)
    try:
        report = assistant.reindex()
        print(f"Корпус: {report.total_files} файлов, {report.total_chunks} фрагментов; "
              f"провайдер: {settings.llm_provider}; порог MIN_COVERAGE={settings.min_coverage}")
        if tune:
            dev_in = [q for q in in_domain if q.split == "dev"]
            dev_off = [q for q in off_topic if q.split == "dev"]
            threshold, score = tune_threshold(assistant, dev_in, dev_off)
            print(f"\nПодбор на dev ({len(dev_in)} по теме, {len(dev_off)} не по теме): "
                  f"порог {threshold} (сбалансированная точность {score:.0%})")
            assistant.settings = dataclasses.replace(settings, min_coverage=threshold)
            assistant.load()
            test_in = [q for q in in_domain if q.split == "test"]
            test_off = [q for q in off_topic if q.split == "test"]
            print(f"\nTest-часть ({len(test_in)} по теме, {len(test_off)} не по теме), порог {threshold}:")
            for line in evaluate(assistant, test_in, test_off, k=k).lines():
                print(line)
            return 0
        print(f"\nВсе вопросы ({len(in_domain)} по теме, {len(off_topic)} не по теме):")
        for line in evaluate(assistant, in_domain, off_topic, k=k).lines():
            print(line)
        return 0
    finally:
        assistant.close()
