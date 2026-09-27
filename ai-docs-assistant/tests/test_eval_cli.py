from __future__ import annotations

from pathlib import Path

from docs_assistant.cli import main
from docs_assistant.evaluation import evaluate, load_questions

QUESTIONS = Path(__file__).resolve().parents[1] / "eval" / "questions.json"


def test_eval_quality_does_not_regress(make_assistant) -> None:
    """Страховка от ухудшений: цифры ниже зафиксированных в README значит, что поиск сломали."""
    in_domain, off_topic = load_questions(QUESTIONS)
    assert len(in_domain) == 20 and len(off_topic) == 10
    report = evaluate(make_assistant(), in_domain, off_topic, k=3)
    assert report.hits_at_k >= 18
    assert report.correct_refusals >= 9
    assert report.false_refusals <= 3


def test_cli_ingest_ask_search(tmp_path: Path, demo_docs: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)  # без .env в рабочей папке
    for name in ("LLM_PROVIDER", "DOCS_DIR", "INDEX_DIR"):
        monkeypatch.delenv(name, raising=False)
    index = str(tmp_path / "index")
    assert main(["--index", index, "ingest", str(demo_docs)]) == 0
    assert "Файлов в индексе: 7" in capsys.readouterr().out

    assert main(["--index", index, "--docs", str(demo_docs), "ask", "Какая", "комиссия", "за", "наложенный", "платёж?"]) == 0
    out = capsys.readouterr().out
    assert "2,5%" in out and "[tarify-2026.pdf, стр. 2]" in out

    assert main(["--index", index, "--docs", str(demo_docs), "ask", "--json", "Как приготовить борщ?"]) == 0
    assert '"status": "no_answer"' in capsys.readouterr().out

    assert main(["--index", index, "--docs", str(demo_docs), "search", "возврат", "посылки"]) == 0
    assert "Решение: есть ответ" in capsys.readouterr().out


def test_cli_config_error_exit_code(capsys, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "unknown")
    assert main(["ask", "вопрос"]) == 2
    assert "LLM_PROVIDER" in capsys.readouterr().err
