"""Общие фикстуры: демо-набор генерируется один раз на всю сессию тестов."""

from __future__ import annotations

from pathlib import Path

import pytest

from doctools.samples import SampleSet, generate


@pytest.fixture(scope="session")
def samples(tmp_path_factory: pytest.TempPathFactory) -> SampleSet:
    return generate(tmp_path_factory.mktemp("samples"))


@pytest.fixture()
def out(tmp_path: Path) -> Path:
    return tmp_path
