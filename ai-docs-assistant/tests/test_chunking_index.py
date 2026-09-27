from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from docs_assistant.chunking import chunk_document, split_text
from docs_assistant.index import IndexBuildError, IndexStore
from docs_assistant.loaders import LoadedDocument, Section


def _sentences(n: int) -> str:
    return " ".join(f"Предложение номер {i} про доставку посылок." for i in range(n))


def test_split_respects_size_and_overlaps() -> None:
    pieces = split_text(_sentences(40), size=200, overlap=60)
    assert len(pieces) > 5
    assert all(len(p) <= 200 for p in pieces)
    for left, right in zip(pieces, pieces[1:]):
        # начало следующего куска повторяет хвост предыдущего целыми предложениями
        first_sentence = right.split(". ")[0]
        assert first_sentence in left


def test_split_without_overlap_and_long_word() -> None:
    pieces = split_text(_sentences(10), size=150, overlap=0)
    assert " ".join(pieces).count("Предложение номер 0 ") == 1
    long_word = split_text("x" * 450, size=200, overlap=50)
    assert [len(p) for p in long_word] == [200, 200, 50]
    with pytest.raises(ValueError):
        split_text("текст", size=100, overlap=100)


def test_chunks_do_not_cross_sections_and_keep_metadata() -> None:
    doc = LoadedDocument(
        title="Тарифы",
        sections=(Section(text=_sentences(12), page=1), Section(text="Короткая вторая страница.", page=2)),
    )
    chunks = chunk_document("t.pdf", doc, size=300, overlap=50, id_prefix="abc")
    assert {c.page for c in chunks} == {1, 2}
    assert chunks[-1].text == "Короткая вторая страница."
    assert len({c.id for c in chunks}) == len(chunks)
    assert "тариф" in chunks[-1].tokens  # заголовок документа участвует в поиске


def test_incremental_reindex(tmp_path: Path, demo_docs: Path) -> None:
    docs = tmp_path / "docs"
    shutil.copytree(demo_docs, docs)
    store = IndexStore(tmp_path / "index")

    first = store.sync(docs)
    assert first.full_rebuild and len(first.added) == 7 and not first.failed

    again = store.sync(docs)
    assert not again.changed and len(again.unchanged) == 7

    faq = docs / "faq.md"
    faq.write_text(faq.read_text(encoding="utf-8") + "\n## Новый вопрос\n\nНовый ответ про упаковку.\n", "utf-8")
    (docs / "prochee" / "pismo-partnera.txt").unlink()
    (docs / "new.txt").write_text("Новый документ\nТекст нового документа.", encoding="utf-8")
    third = store.sync(docs)
    assert third.updated == ["faq.md"]
    assert third.removed == ["prochee/pismo-partnera.txt"]
    assert third.added == ["new.txt"]
    assert len(third.unchanged) == 5
    assert any("Новый ответ про упаковку" in c.text for c in store.load_chunks())


def test_touched_file_is_not_reparsed(tmp_path: Path, demo_docs: Path) -> None:
    docs = tmp_path / "docs"
    shutil.copytree(demo_docs, docs)
    store = IndexStore(tmp_path / "index")
    store.sync(docs)
    target = docs / "tarify-2026.pdf"
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    report = store.sync(docs)
    assert not report.changed and "tarify-2026.pdf" in report.unchanged


def test_params_change_forces_full_rebuild(tmp_path: Path, demo_docs: Path) -> None:
    IndexStore(tmp_path / "index", chunk_size=800).sync(demo_docs)
    report = IndexStore(tmp_path / "index", chunk_size=500, chunk_overlap=100).sync(demo_docs)
    assert report.full_rebuild and len(report.added) == 7


def test_bad_file_is_reported_not_fatal(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ok.md").write_text("# Документ\n\nТекст.", encoding="utf-8")
    (docs / "broken.pdf").write_bytes(b"not a pdf")
    (docs / "~$temp.docx").write_bytes(b"lock file")
    report = IndexStore(tmp_path / "index").sync(docs)
    assert report.added == ["ok.md"]
    assert "broken.pdf" in report.failed


def test_missing_docs_dir(tmp_path: Path) -> None:
    with pytest.raises(IndexBuildError):
        IndexStore(tmp_path / "index").sync(tmp_path / "nope")
