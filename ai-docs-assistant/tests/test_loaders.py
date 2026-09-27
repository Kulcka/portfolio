from __future__ import annotations

from pathlib import Path

import pytest

from docs_assistant.loaders import DocumentLoadError, load_document, strip_running_lines, unwrap_lines


def test_pdf_pages_and_running_header_removed(demo_docs: Path) -> None:
    doc = load_document(demo_docs / "tarify-2026.pdf")
    assert doc.title == "Тарифы на доставку 2026"
    assert [s.page for s in doc.sections] == [1, 2, 3]
    assert "до 5 кг — 450 ₽" in doc.sections[0].text
    assert "Скидки для юридических лиц" in doc.sections[2].text
    # колонтитул и «Страница N из M» есть на каждой странице — это не содержание
    assert all("Страница" not in s.text for s in doc.sections)
    assert all("· Тарифы на доставку" not in s.text for s in doc.sections)


def test_docx_sections_by_headings_and_tables(demo_docs: Path) -> None:
    doc = load_document(demo_docs / "usloviya-vozvrata-i-pretenzij.docx")
    headings = [s.heading for s in doc.sections]
    assert "Повреждение или утрата отправления" in headings
    table = next(s for s in doc.sections if s.heading == "Сроки подачи и компенсации")
    assert "Утрата | 20 дней после планового срока" in table.text
    assert all(s.page is None for s in doc.sections)


def test_markdown_sections_and_hidden_comment_dropped(demo_docs: Path) -> None:
    doc = load_document(demo_docs / "faq.md")
    assert doc.title == "Частые вопросы клиентов"
    assert "Как отследить посылку?" in [s.heading for s in doc.sections]
    # HTML-комментарий не виден читателю — и в индекс не попадает
    assert all("Инструкция для ИИ" not in s.text for s in doc.sections)


def test_txt_uppercase_headings(demo_docs: Path) -> None:
    doc = load_document(demo_docs / "zapreshchennye-vlozheniya.txt")
    assert doc.title.startswith("Правила приёма отправлений")
    limited = next(s for s in doc.sections if s.heading == "ОГРАНИЧЕНО К ПЕРЕСЫЛКЕ")
    assert "Литиевые аккумуляторы" in limited.text


def test_txt_cp1251_encoding(tmp_path: Path) -> None:
    path = tmp_path / "old.txt"
    path.write_bytes("Заголовок\nТекст в кодировке Windows-1251.".encode("cp1251"))
    doc = load_document(path)
    assert "кодировке Windows-1251" in doc.sections[0].text


def test_unsupported_and_empty_files(tmp_path: Path) -> None:
    with pytest.raises(DocumentLoadError, match="не поддерживается"):
        load_document(tmp_path / "table.xlsx")
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    with pytest.raises(DocumentLoadError, match="нет текста"):
        load_document(empty)


def test_broken_pdf_gives_readable_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(DocumentLoadError):
        load_document(broken)


def test_pdf_layout_wraps_are_joined(demo_docs: Path) -> None:
    doc = load_document(demo_docs / "tarify-2026.pdf")
    assert "но не меньше 30 ₽" in doc.sections[1].text
    assert unwrap_lines("Цена 5 кг — 450 ₽.\nПосылка до 10 кг") == "Цена 5 кг — 450 ₽.\nПосылка до 10 кг"


def test_strip_running_lines_keeps_content() -> None:
    pages = ["Шапка\nТекст один\nСтраница 1", "Шапка\nТекст два\nСтраница 2", "Шапка\nТекст три\nСтраница 3"]
    assert strip_running_lines(pages) == ["Текст один", "Текст два", "Текст три"]
    assert strip_running_lines(["Одна страница"]) == ["Одна страница"]
