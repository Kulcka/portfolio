"""Тесты с настоящим Microsoft Word через COM. Запуск: pytest -m word"""

import sys

import pdfplumber
import pytest

from doctools.cli import main
from doctools.word_com import WordSession, winword_pids

pytestmark = [pytest.mark.word, pytest.mark.skipif(sys.platform != "win32", reason="нужен Windows с Word")]


def test_docx_to_pdf_and_word_closed(samples, out):
    before = winword_pids()
    with WordSession() as word:
        assert word.own_pids, "не удалось определить PID запущенного Word"
        own = set(word.own_pids)
        pdf = word.to_pdf(samples.letter_docx, out / "шаблон.pdf")
    assert pdf.read_bytes()[:5] == b"%PDF-"
    with pdfplumber.open(pdf) as doc:
        text = doc.pages[0].extract_text()
    assert "О продлении договора" in text
    assert not (own & winword_pids()), "наш WINWORD.EXE остался висеть"
    assert winword_pids() == before                                       # чужие экземпляры Word не тронуты


def test_word_closed_after_error(out):
    with pytest.raises(Exception):
        with WordSession() as word:
            own = set(word.own_pids)
            word.to_pdf(out / "нет_такого.docx", out / "x.pdf")
    assert not (own & winword_pids())


def test_cli_fill_with_pdf_and_convert(samples, out):
    before = winword_pids()
    assert main(["docx-fill", str(samples.letter_docx), str(samples.recipients_xlsx), "-o", str(out / "l"),
                 "--name", "Письмо_{{Номер договора}}", "--pdf"]) == 0
    pdfs = sorted((out / "l").glob("*.pdf"))
    assert len(pdfs) == 5
    with pdfplumber.open(pdfs[0]) as doc:
        assert "Иванова А. С." in doc.pages[0].extract_text()
    assert main(["convert", str(out / "l"), "--to", "pdf", "-o", str(out / "p")]) == 0
    assert len(list((out / "p").glob("*.pdf"))) == 5
    assert winword_pids() == before                                       # после двух запусков Word не висит
