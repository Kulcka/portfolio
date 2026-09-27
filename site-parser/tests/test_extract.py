"""Разбор полей на сохранённых страницах books.toscrape.com и quotes.toscrape.com."""

from __future__ import annotations

from bs4 import BeautifulSoup

from site_parser.config import FieldSpec, SiteConfig, SpecsSpec
from site_parser.extract import extract_field, extract_specs, missing_required
from site_parser.fetcher import Page
from site_parser.scraper import Scraper
from tests.conftest import BOOKS, QUOTES, fixture_bytes

CATALOG_URL = f"{BOOKS}/catalogue/page-1.html"
PRODUCT_URL = f"{BOOKS}/catalogue/a-light-in-the-attic_1000/index.html"


def _page(relative: str, url: str, encoding: str | None = None) -> BeautifulSoup:
    return Page(url=url, status=200, content=fixture_bytes(relative), encoding=encoding).soup()


def test_books_list_page_fields(books_config: SiteConfig) -> None:
    scraper = Scraper(books_config, fetcher=None)  # type: ignore[arg-type]
    items = scraper.parse_list_page(_page("books/catalogue_page-1.html", CATALOG_URL), CATALOG_URL)

    assert len(items) == 20
    first = items[0]
    assert first == {
        "title": "A Light in the Attic",  # полное название из атрибута title, а не обрезанный текст
        "price": 51.77,
        "currency": "GBP",  # «£» без charset в заголовке — кодировка взята из <meta>
        "in_stock": True,
        "rating": 3,  # класс «star-rating Three» → регулярка → map → int
        "url": PRODUCT_URL,
    }
    assert all(isinstance(item["price"], float) for item in items)
    assert {item["rating"] for item in items} <= {1, 2, 3, 4, 5}


def test_books_detail_page_fields_and_specs(books_config: SiteConfig) -> None:
    scraper = Scraper(books_config, fetcher=None)  # type: ignore[arg-type]
    item = scraper.parse_detail_page(_page("books/product_a-light-in-the-attic.html", PRODUCT_URL), PRODUCT_URL)

    assert item["upc"] == "a897fe39b1053632"
    assert item["category"] == "Poetry"
    assert item["stock_qty"] == 22
    assert item["description"].startswith("It's hard to imagine a world without A Light in the Attic")
    assert item["image_url"] == f"{BOOKS}/media/cache/fe/72/fe72f0532301ec28892ae79a629a293c.jpg"
    # Характеристики из таблицы: UPC и Availability исключены в конфиге
    assert item["Характеристика: Product Type"] == "Books"
    assert item["Характеристика: Price (incl. tax)"] == "£51.77"
    assert item["Характеристика: Number of reviews"] == "0"
    assert "Характеристика: UPC" not in item
    assert "Характеристика: Availability" not in item


def test_quotes_list_page_fields(quotes_config: SiteConfig) -> None:
    url = f"{QUOTES}/page/1/"
    scraper = Scraper(quotes_config, fetcher=None)  # type: ignore[arg-type]
    items = scraper.parse_list_page(_page("quotes/page-1.html", url, "utf-8"), url)

    assert len(items) == 10
    first = items[0]
    assert first["author"] == "Albert Einstein"
    assert first["text"].startswith("The world as we have created it")
    assert not first["text"].startswith("“") and not first["text"].endswith("”")
    assert first["tags"] == ["change", "deep-thoughts", "thinking", "world"]
    assert first["author_url"] == f"{QUOTES}/author/Albert-Einstein"


HTML = """
<div class="card" data-id="42">
  <h2 class="name">  Смартфон   X  </h2>
  <span class="price">Цена: 12 990 ₽</span>
  <span class="old">15 000 ₽</span>
  <span class="stock">Нет в наличии</span>
  <img class="photo" src="/img/1.jpg"><img class="photo" src="/img/2.jpg">
  <div class="desc"><p>Строка <b>жирная</b></p></div>
</div>
"""
BASE = "https://shop.example/catalog/"


def _root() -> BeautifulSoup:
    return BeautifulSoup(HTML, "lxml")


def test_text_attr_regex_and_types() -> None:
    root = _root()
    assert extract_field(root, FieldSpec("name", css="h2.name"), BASE) == "Смартфон X"
    assert extract_field(root, FieldSpec("price", css="span.price", type="price"), BASE) == 12990.0
    assert extract_field(root, FieldSpec("cur", css="span.price", type="currency"), BASE) == "RUB"
    assert extract_field(root, FieldSpec("stock", css="span.stock", type="availability"), BASE) is False
    assert extract_field(root, FieldSpec("id", css="div.card", attr="data-id", type="int"), BASE) == 42
    assert extract_field(root, FieldSpec("old", css="span.old", regex=r"(\d[\d ]*)", type="int"), BASE) == 15000


def test_src_attribute_becomes_absolute_and_multiple_gives_list() -> None:
    root = _root()
    single = extract_field(root, FieldSpec("img", css="img.photo", attr="src"), BASE)
    assert single == "https://shop.example/img/1.jpg"
    many = extract_field(root, FieldSpec("imgs", css="img.photo", attr="src", multiple=True), BASE)
    assert many == ["https://shop.example/img/1.jpg", "https://shop.example/img/2.jpg"]


def test_html_attr_keeps_markup() -> None:
    assert extract_field(_root(), FieldSpec("d", css="div.desc", attr="html"), BASE) == "<p>Строка <b>жирная</b></p>"


def test_missing_field_uses_default_and_required_is_reported() -> None:
    root = _root()
    spec = FieldSpec("sku", css="span.sku", default="нет артикула")
    assert extract_field(root, spec, BASE) == "нет артикула"
    no_match = FieldSpec("n", css="h2.name", regex=r"(\d+)", type="int")
    assert extract_field(root, no_match, BASE) is None
    required = FieldSpec("sku", css="span.sku", required=True)
    assert missing_required({"sku": None}, [required]) == ["sku"]
    assert missing_required({"sku": "A-1"}, [required]) == []


def test_map_is_case_insensitive_and_unknown_stays() -> None:
    root = BeautifulSoup('<p class="r">three</p><p class="q">Много</p>', "lxml")
    rating = FieldSpec("r", css="p.r", map={"Three": 3}, type="int")
    assert extract_field(root, rating, BASE) == 3
    stock = FieldSpec("q", css="p.q", map={"мало": False})
    assert extract_field(root, stock, BASE) == "Много"


def test_specs_pairs_mode_for_dl_lists() -> None:
    root = BeautifulSoup(
        "<dl><dt>Цвет:</dt><dd>чёрный</dd><dt>Вес</dt><dd>180 г</dd><dt>Гарантия</dt><dd>1 год</dd></dl>", "lxml"
    )
    specs = extract_specs(root, SpecsSpec(keys="dl dt", values="dl dd", exclude=("Гарантия",)))
    assert specs == {"Цвет": "чёрный", "Вес": "180 г"}
    only = extract_specs(root, SpecsSpec(keys="dl dt", values="dl dd", include=("Вес",), prefix="Х: "))
    assert only == {"Х: Вес": "180 г"}
