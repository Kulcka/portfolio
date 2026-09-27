from __future__ import annotations

from docs_assistant.bm25 import BM25
from docs_assistant.textproc import Synonyms, tokenize


def test_tokenize_russian_normalization() -> None:
    tokens = tokenize("Сколько СТОИТ доставка посылок? Ёмкость 100 Вт·ч")
    assert "сколько" not in tokens and "скольк" not in tokens  # стоп-слово
    assert "доставк" in tokens and "100" in tokens
    assert "емкост" in tokens  # ё → е, нижний регистр, основа


def test_synonyms_expand_and_skip_phrases() -> None:
    synonyms = Synonyms.parse("цена, стоимость, тариф\nстрахование, объявленная ценность\n# комментарий")
    assert tokenize("стоимость")[0] in synonyms.expand(tokenize("цена")[0])
    # словосочетание пропущено: «ценность» не должна стать синонимом «страхования»
    assert synonyms.expand(tokenize("страхование")[0]) == frozenset(tokenize("страхование"))


def test_bm25_ranks_and_prefix_variants() -> None:
    docs = [tokenize("Задержка доставки посылки"), tokenize("Упаковка хрупких вещей"), tokenize("Тарифы на доставку")]
    bm25 = BM25(docs)
    scores = bm25.score_groups([{t} for t in tokenize("упаковка")])
    assert max(scores, key=scores.get) == 1
    # «задержали» → «задержа», в тексте «задержк»: формы одного слова
    assert "задержк" in bm25.prefix_variants(tokenize("задержали")[0])
    # короткая основа не расширяется
    assert bm25.prefix_variants("цен") == set()


def test_search_finds_expected_sources(make_assistant) -> None:
    assistant = make_assistant()
    cases = {
        "Сколько стоит отправить посылку весом 4 кг?": ("tarify-2026.pdf", 1),
        "Сколько стоит застраховать посылку?": ("tarify-2026.pdf", 2),
        "Вы доставляете посылки в Казахстан?": ("tarify-2026.pdf", 3),
    }
    for question, (doc, page) in cases.items():
        result = assistant.search(question, k=3)
        assert result.relevant, question
        assert any(h.chunk.doc == doc and h.chunk.page == page for h in result.hits), question
    docx_hit = assistant.search("До скольки работает поддержка в воскресенье?", k=1).hits[0].chunk
    assert docx_hit.doc == "reglament-sluzhby-podderzhki.docx"
    assert docx_hit.section == "Режим работы службы поддержки"


def test_off_topic_is_not_relevant(make_assistant) -> None:
    assistant = make_assistant()
    for question in ("Как приготовить борщ?", "Сколько стоит доставка пиццы?", "Какой сегодня курс доллара?"):
        assert not assistant.search(question).relevant, question


def test_hybrid_search_with_embeddings(make_assistant) -> None:
    """Эмбеддинги подмешиваются через RRF; при сбое сервиса поиск падает обратно на BM25."""
    from docs_assistant.llm.base import EmbeddingProvider, LLMError

    class KeywordEmbeddings(EmbeddingProvider):
        vocabulary = ("отслед", "трек", "статус", "посылк", "где")

        def __init__(self) -> None:
            self.fail = False

        @property
        def model_id(self) -> str:
            return "test-keywords"

        def _vec(self, text: str) -> list[float]:
            low = text.lower()
            return [float(low.count(word)) for word in self.vocabulary] + [0.01]

        def embed_documents(self, texts):
            return [self._vec(t) for t in texts]

        def embed_query(self, text: str) -> list[float]:
            if self.fail:
                raise LLMError("embeddings: недоступно")
            return self._vec(text + " отслед трек статус")

    def rank_of_tracking(result) -> int:
        return [h.chunk.section for h in result.hits].index("Как отследить посылку?")

    question = "Где сейчас моя посылка?"  # слова «отследить» в вопросе нет — BM25 его не видит
    assistant = make_assistant()
    bm25_rank = rank_of_tracking(assistant.search(question, k=100))

    embedder = KeywordEmbeddings()
    assistant.embeddings = embedder
    assistant.load()
    assert assistant.retriever.hybrid
    hybrid = assistant.search(question, k=100)
    assert all(h.similarity is not None for h in hybrid.hits)
    assert rank_of_tracking(hybrid) < bm25_rank  # эмбеддинги подняли нужный раздел

    embedder.fail = True
    fallback = assistant.search("Сколько стоит застраховать посылку?", k=3)
    assert fallback.hits and all(h.similarity is None for h in fallback.hits)
