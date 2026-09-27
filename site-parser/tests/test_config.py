"""Загрузка и проверка YAML-конфигов: понятные ошибки вместо молчаливых опечаток."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from site_parser.config import ConfigError, SiteConfig, load_config, parse_config
from tests.conftest import CONFIGS


def _minimal(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "shop",
        "start_urls": ["https://shop.test/catalog"],
        "list": {"item": "div.card", "fields": {"title": "h2", "price": {"css": ".price", "type": "price"}}},
    }
    data.update(extra)
    return data


def test_demo_configs_load(books_config: SiteConfig, quotes_config: SiteConfig) -> None:
    assert books_config.name == "books_toscrape"
    assert books_config.request.respect_robots is True
    assert books_config.request.delay >= 1
    assert books_config.changes.key == ("upc",)
    assert books_config.price_field() == "price" and books_config.title_field() == "title"
    headers = [c.header for c in books_config.static_columns()]
    assert headers[:3] == ["Название", "Цена", "Валюта"] and headers[-2:] == ["Ссылка", "Файл картинки"]
    assert books_config.output.google_sheets.enabled is False  # GOOGLE_SHEET_ID не задан

    assert quotes_config.start_urls == ("https://quotes.toscrape.com/page/1/",)  # из шаблона
    assert quotes_config.changes.key == ("author", "text")


def test_all_config_files_in_repo_are_valid() -> None:
    for path in CONFIGS.glob("*.yaml"):
        load_config(path)


def test_defaults_and_shorthand() -> None:
    config = parse_config(_minimal())
    assert config.listing is not None
    title = config.listing.fields[0]
    assert (title.css, title.attr, title.type) == ("h2", "text", "str")
    assert config.output.dir == "output/shop"
    assert config.output.formats == ("csv", "xlsx")
    assert config.request.retries == 3 and config.request.respect_robots


def test_type_aliases() -> None:
    config = parse_config(_minimal(list={"item": "li", "fields": {"n": {"css": "b", "type": "number"}}}))
    assert config.listing and config.listing.fields[0].type == "float"


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"name": "my shop!"}, "name: только латиница"),
        ({"start_urls": ["shop.test"]}, "http://"),
        ({"selecter": "x"}, "неизвестные ключи selecter"),
        (
            {"list": {"item": "div.card", "fields": {"price": {"css": ".p", "type": "prise"}}}},
            "неизвестный тип 'prise'",
        ),
        ({"list": {"item": "div.card", "fields": {"price": {"css": ".p", "atr": "href"}}}}, "неизвестные ключи atr"),
        ({"list": {"item": "div[", "fields": {"t": "h2"}}}, "неверный CSS-селектор"),
        (
            {"list": {"item": "div", "fields": {"t": {"css": "h2", "regex": "(unclosed"}}}},
            "неверное регулярное выражение",
        ),
        ({"list": {"item": "div", "fields": {"url": "a"}}}, "зарезервировано"),
        ({"pagination": {"next": "a.next", "template": "https://s.test/?p={page}"}}, "что-то одно"),
        ({"pagination": {"template": "https://s.test/?p=1"}}, "{page}"),
        ({"detail": {"fields": {"sku": ".sku"}}}, "list.link"),
        ({"changes": {"key": "sku"}}, "changes.key: поля 'sku' нет"),
        ({"output": {"formats": ["xls"]}}, "неизвестные форматы xls"),
        ({"request": {"delay": "быстро"}}, "ожидается число"),
        ({"request": {"respect_robots": "yes"}}, "true или false"),
    ],
)
def test_validation_errors(patch: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        parse_config(_minimal(**patch))


def test_missing_name_and_sections() -> None:
    with pytest.raises(ConfigError, match="name: обязательный"):
        parse_config({"start_urls": ["https://s.test"], "list": {"item": "a", "fields": {"t": "b"}}})
    with pytest.raises(ConfigError, match="нужна секция list"):
        parse_config({"name": "x", "start_urls": ["https://s.test"]})


def test_env_interpolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOP_HOST", "https://shop.test")
    config = parse_config(
        _minimal(
            start_urls=["${SHOP_HOST}/catalog"],
            output={"google_sheets": {"spreadsheet": "${SHEET_ID:-}"}},
        )
    )
    assert config.start_urls == ("https://shop.test/catalog",)
    assert config.output.google_sheets.enabled is False
    with pytest.raises(ConfigError, match="переменная окружения MISSING_VAR"):
        parse_config(_minimal(title="${MISSING_VAR}"))


def test_start_urls_file_relative_to_config(tmp_path: Path) -> None:
    (tmp_path / "urls.txt").write_text("# товары\nhttps://shop.test/p/1\n\nhttps://shop.test/p/2\n", encoding="utf-8")
    (tmp_path / "shop.yaml").write_text(
        "name: shop\nstart_urls_file: urls.txt\ndetail:\n  fields:\n    title: h1\n", encoding="utf-8"
    )
    config = load_config(tmp_path / "shop.yaml")
    assert config.start_urls == ("https://shop.test/p/1", "https://shop.test/p/2")
    assert config.listing is None and config.detail is not None


def test_yaml_syntax_error_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("name: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="синтаксиса YAML"):
        load_config(path)
