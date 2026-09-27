from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest

from docs_assistant.assistant import DocsAssistant
from docs_assistant.config import Settings
from docs_assistant.llm.base import LLMProvider
from docs_assistant.llm.fake import ExtractiveProvider

ROOT = Path(__file__).resolve().parents[1]
DEMO_DOCS = ROOT / "demo_docs"


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """CLI настраивает корневой логгер; после теста возвращаем как было."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def demo_docs() -> Path:
    return DEMO_DOCS


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Настройки без .env и переменных окружения машины: тесты не зависят от окружения."""
    base = Settings.from_env({}, base_dir=ROOT)
    return dataclasses.replace(base, docs_dir=DEMO_DOCS, index_dir=tmp_path / "index")


@pytest.fixture
def make_assistant(settings: Settings):
    created: list[DocsAssistant] = []

    def factory(llm: LLMProvider | None = None, **overrides) -> DocsAssistant:
        assistant = DocsAssistant(dataclasses.replace(settings, **overrides), llm or ExtractiveProvider())
        assistant.reindex()
        created.append(assistant)
        return assistant

    yield factory
    for assistant in created:
        assistant.close()
