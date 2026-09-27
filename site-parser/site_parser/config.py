"""Загрузка и проверка YAML-конфига сайта.

Конфиг описывает всё, что меняется от сайта к сайту: стартовые адреса,
селекторы, пагинацию, поля, выгрузку. Код парсера под новый сайт не меняется.
Ошибки конфига сообщаются по-русски с путём до ключа
(``list.fields.price: неизвестный тип 'prise'``), опечатки в ключах не
проходят молча.

В строках можно подставлять переменные окружения: ``${GOOGLE_SHEET_ID}`` —
обязательная, ``${GOOGLE_SHEET_ID:-}`` — со значением по умолчанию.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import soupsieve
import yaml

from site_parser.converters import CONVERTERS

DEFAULT_USER_AGENT = "SiteParser/1.0 (+https://github.com/; polite crawler)"
URL_COLUMN = "url"

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_TYPE_ALIASES = {
    "text": "str",
    "string": "str",
    "number": "float",
    "integer": "int",
    "stock": "availability",
    "link": "url",
    "boolean": "bool",
}
OUTPUT_FORMATS = ("csv", "xlsx", "json")


class ConfigError(Exception):
    """Ошибка в YAML-конфиге. Текст пригоден для показа пользователю."""


# --------------------------------------------------------------------------- #
# Модель конфига
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FieldSpec:
    """Одно поле: откуда взять и к чему привести."""

    name: str
    css: str | None = None
    attr: str = "text"
    regex: str | None = None
    type: str = "str"
    multiple: bool = False
    default: Any = None
    required: bool = False
    map: Mapping[str, Any] | None = None
    label: str | None = None

    @property
    def header(self) -> str:
        return self.label or self.name


@dataclass(frozen=True)
class SpecsSpec:
    """Таблица характеристик произвольного состава → отдельные колонки."""

    rows: str | None = None
    key: str = "th"
    value: str = "td"
    keys: str | None = None
    values: str | None = None
    prefix: str = ""
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class ListSpec:
    item: str
    link: str | None
    fields: tuple[FieldSpec, ...]


@dataclass(frozen=True)
class DetailSpec:
    fields: tuple[FieldSpec, ...]
    specs: SpecsSpec | None = None


@dataclass(frozen=True)
class PaginationSpec:
    next: str | None = None
    template: str | None = None
    start: int = 1
    max_pages: int | None = None


@dataclass(frozen=True)
class RequestSettings:
    delay: float = 1.0
    jitter: float = 0.0
    timeout: float = 20.0
    retries: int = 3
    backoff: float = 2.0
    backoff_max: float = 60.0
    user_agent: str = DEFAULT_USER_AGENT
    respect_robots: bool = True
    cache: bool = False
    cache_dir: str = ".cache/pages"
    cache_ttl_hours: float | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    verify_ssl: bool = True


@dataclass(frozen=True)
class ImagesSpec:
    field: str
    dir: str = "images"
    max: int | None = None
    column: str = "image_file"
    label: str = "Файл картинки"


@dataclass(frozen=True)
class GoogleSheetsSpec:
    spreadsheet: str = ""
    worksheet: str = "Данные"
    changes_worksheet: str | None = "Изменения"

    @property
    def enabled(self) -> bool:
        return bool(self.spreadsheet.strip())


@dataclass(frozen=True)
class OutputSpec:
    dir: str
    basename: str
    formats: tuple[str, ...] = ("csv", "xlsx")
    sheet_name: str = "Данные"
    csv_delimiter: str = ";"
    list_separator: str = ", "
    bool_values: tuple[str, str] = ("да", "нет")
    url_label: str = "Ссылка"
    google_sheets: GoogleSheetsSpec = field(default_factory=GoogleSheetsSpec)


@dataclass(frozen=True)
class ChangesSpec:
    """Как сравнивать прогоны: ключ записи, поле цены, отслеживаемые поля."""

    key: tuple[str, ...] = (URL_COLUMN,)
    title_field: str | None = None
    price_field: str | None = None
    track: tuple[str, ...] = ()
    gone_guard_ratio: float = 0.5


@dataclass(frozen=True)
class TelegramSpec:
    enabled: bool = False
    only_changes: bool = True
    max_lines: int = 10


@dataclass(frozen=True)
class StorageSpec:
    path: str = "state/site_parser.sqlite3"
    keep_runs: int = 30


@dataclass(frozen=True)
class Column:
    """Колонка выгрузки: имя поля, заголовок, вид значения (для форматирования)."""

    name: str
    header: str
    kind: str = "str"


@dataclass(frozen=True)
class SiteConfig:
    name: str
    title: str
    start_urls: tuple[str, ...]
    request: RequestSettings
    output: OutputSpec
    changes: ChangesSpec
    telegram: TelegramSpec
    storage: StorageSpec
    listing: ListSpec | None = None
    detail: DetailSpec | None = None
    pagination: PaginationSpec | None = None
    images: ImagesSpec | None = None
    max_items: int | None = None
    source: Path | None = None

    def all_fields(self) -> tuple[FieldSpec, ...]:
        """Поля списка и карточки без повторов (поле карточки главнее)."""
        by_name: dict[str, FieldSpec] = {}
        for spec in self.listing.fields if self.listing else ():
            by_name[spec.name] = spec
        for spec in self.detail.fields if self.detail else ():
            by_name[spec.name] = spec
        return tuple(by_name.values())

    def static_columns(self) -> list[Column]:
        """Колонки, известные до прогона: поля, ссылка, файл картинки."""
        columns = [Column(spec.name, spec.header, "list" if spec.multiple else spec.type) for spec in self.all_fields()]
        columns.append(Column(URL_COLUMN, self.output.url_label, "url"))
        if self.images:
            columns.append(Column(self.images.column, self.images.label, "list"))
        return columns

    def title_field(self) -> str | None:
        """Поле с названием записи для отчётов и уведомлений."""
        if self.changes.title_field:
            return self.changes.title_field
        names = [spec.name for spec in self.all_fields()]
        for candidate in ("title", "name", "название", "text"):
            if candidate in names:
                return candidate
        str_fields = [s.name for s in self.all_fields() if s.type == "str" and not s.multiple]
        return str_fields[0] if str_fields else None

    def label_for(self, name: str) -> str:
        """Подпись поля для отчётов: label из конфига или само имя."""
        for spec in self.all_fields():
            if spec.name == name:
                return spec.header
        return name

    def price_field(self) -> str | None:
        if self.changes.price_field:
            return self.changes.price_field
        prices = [spec.name for spec in self.all_fields() if spec.type == "price"]
        return prices[0] if prices else None


# --------------------------------------------------------------------------- #
# Чтение секций с проверкой
# --------------------------------------------------------------------------- #


class _Section:
    """Обёртка над словарём секции: типизированное чтение и контроль лишних ключей."""

    def __init__(self, data: Any, path: str) -> None:
        if data is None:
            data = {}
        if not isinstance(data, Mapping):
            raise ConfigError(f"{path}: ожидается набор «ключ: значение», получено {_kind(data)}")
        self.data: dict[str, Any] = dict(data)
        self.path = path
        self._used: set[str] = set()

    def _p(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def has(self, key: str) -> bool:
        return self.data.get(key) is not None

    def raw(self, key: str, default: Any = None) -> Any:
        self._used.add(key)
        value = self.data.get(key)
        return default if value is None else value

    def text(self, key: str, default: str | None = None, *, required: bool = False) -> str | None:
        value = self.raw(key)
        if value is None:
            if required:
                raise ConfigError(f"{self._p(key)}: обязательный ключ не задан")
            return default
        if isinstance(value, (dict, list)):
            raise ConfigError(f"{self._p(key)}: ожидается строка, получено {_kind(value)}")
        return str(value)

    def req_text(self, key: str) -> str:
        value = self.text(key, required=True)
        assert value is not None
        return value

    def flag(self, key: str, default: bool) -> bool:
        value = self.raw(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise ConfigError(f"{self._p(key)}: ожидается true или false, получено {value!r}")
        return value

    def number(self, key: str, default: float, *, minimum: float = 0.0) -> float:
        value = self.raw(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{self._p(key)}: ожидается число, получено {value!r}")
        if value < minimum:
            raise ConfigError(f"{self._p(key)}: значение должно быть не меньше {minimum}")
        return float(value)

    def opt_number(self, key: str, *, minimum: float = 0.0) -> float | None:
        if not self.has(key):
            self._used.add(key)
            return None
        return self.number(key, 0.0, minimum=minimum)

    def integer(self, key: str, default: int, *, minimum: int = 0) -> int:
        value = self.raw(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{self._p(key)}: ожидается целое число, получено {value!r}")
        if value < minimum:
            raise ConfigError(f"{self._p(key)}: значение должно быть не меньше {minimum}")
        return value

    def opt_integer(self, key: str, *, minimum: int = 1) -> int | None:
        if not self.has(key):
            self._used.add(key)
            return None
        return self.integer(key, 0, minimum=minimum)

    def text_list(self, key: str) -> tuple[str, ...]:
        value = self.raw(key)
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        if not isinstance(value, list) or not all(isinstance(v, (str, int, float)) for v in value):
            raise ConfigError(f"{self._p(key)}: ожидается строка или список строк")
        return tuple(str(v) for v in value)

    def section(self, key: str) -> _Section | None:
        value = self.raw(key)
        return None if value is None else _Section(value, self._p(key))

    def finish(self) -> None:
        unknown = sorted(set(self.data) - self._used)
        if unknown:
            allowed = ", ".join(sorted(self._used))
            where = self.path or "верхний уровень"
            raise ConfigError(f"{where}: неизвестные ключи {', '.join(unknown)} (допустимы: {allowed})")


def _kind(value: Any) -> str:
    if isinstance(value, Mapping):
        return "словарь"
    if isinstance(value, list):
        return "список"
    return f"значение {value!r}"


def _check_css(selector: str, path: str) -> str:
    try:
        soupsieve.compile(selector)
    except Exception as exc:  # soupsieve бросает SelectorSyntaxError и ValueError
        raise ConfigError(f"{path}: неверный CSS-селектор {selector!r}: {exc}") from None
    return selector


def _check_regex(pattern: str, path: str) -> str:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"{path}: неверное регулярное выражение {pattern!r}: {exc}") from None
    return pattern


# --------------------------------------------------------------------------- #
# Разбор секций
# --------------------------------------------------------------------------- #


def _parse_field(name: str, raw: Any, path: str) -> FieldSpec:
    if not _FIELD_NAME_RE.match(name):
        raise ConfigError(f"{path}: имя поля — латиница, цифры и «_», начинается с буквы")
    if name == URL_COLUMN:
        raise ConfigError(f"{path}: имя «url» зарезервировано — ссылка на карточку добавляется сама")
    if isinstance(raw, str):
        return FieldSpec(name=name, css=_check_css(raw, path))
    sec = _Section(raw, path)
    css = sec.text("css") or sec.text("selector")
    if css is not None:
        _check_css(css, f"{path}.css")
    type_name = (sec.text("type", "str") or "str").strip().lower()
    type_name = _TYPE_ALIASES.get(type_name, type_name)
    if type_name not in CONVERTERS:
        raise ConfigError(f"{path}.type: неизвестный тип {type_name!r} (допустимы: {', '.join(CONVERTERS)})")
    regex = sec.text("regex")
    if regex is not None:
        _check_regex(regex, f"{path}.regex")
    mapping = sec.raw("map")
    if mapping is not None:
        if not isinstance(mapping, Mapping):
            raise ConfigError(f"{path}.map: ожидается словарь «было: стало»")
        mapping = {str(k): v for k, v in mapping.items()}
    spec = FieldSpec(
        name=name,
        css=css,
        attr=sec.text("attr", "text") or "text",
        regex=regex,
        type=type_name,
        multiple=sec.flag("multiple", False),
        default=sec.raw("default"),
        required=sec.flag("required", False),
        map=mapping,
        label=sec.text("label"),
    )
    sec.finish()
    return spec


def _parse_fields(sec: _Section, key: str = "fields") -> tuple[FieldSpec, ...]:
    raw = sec.raw(key)
    path = f"{sec.path}.{key}"
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{path}: ожидается словарь «имя_поля: описание»")
    return tuple(_parse_field(str(name), value, f"{path}.{name}") for name, value in raw.items())


def _parse_specs(sec: _Section) -> SpecsSpec:
    rows, keys, values = sec.text("rows"), sec.text("keys"), sec.text("values")
    if bool(rows) == bool(keys and values):
        raise ConfigError(f"{sec.path}: задайте либо rows (+ key/value), либо пару keys и values")
    for name, selector in (("rows", rows), ("keys", keys), ("values", values)):
        if selector:
            _check_css(selector, f"{sec.path}.{name}")
    spec = SpecsSpec(
        rows=rows,
        key=_check_css(sec.text("key", "th") or "th", f"{sec.path}.key"),
        value=_check_css(sec.text("value", "td") or "td", f"{sec.path}.value"),
        keys=keys,
        values=values,
        prefix=sec.text("prefix", "") or "",
        include=sec.text_list("include"),
        exclude=sec.text_list("exclude"),
    )
    sec.finish()
    return spec


def _parse_request(sec: _Section | None) -> RequestSettings:
    if sec is None:
        return RequestSettings()
    headers = sec.raw("headers", {})
    if not isinstance(headers, Mapping):
        raise ConfigError(f"{sec.path}.headers: ожидается словарь «заголовок: значение»")
    settings = RequestSettings(
        delay=sec.number("delay", 1.0),
        jitter=sec.number("jitter", 0.0),
        timeout=sec.number("timeout", 20.0, minimum=1.0),
        retries=sec.integer("retries", 3),
        backoff=sec.number("backoff", 2.0),
        backoff_max=sec.number("backoff_max", 60.0),
        user_agent=sec.text("user_agent", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT,
        respect_robots=sec.flag("respect_robots", True),
        cache=sec.flag("cache", False),
        cache_dir=sec.text("cache_dir", ".cache/pages") or ".cache/pages",
        cache_ttl_hours=sec.opt_number("cache_ttl_hours"),
        headers={str(k): str(v) for k, v in headers.items()},
        verify_ssl=sec.flag("verify_ssl", True),
    )
    sec.finish()
    return settings


def _parse_output(sec: _Section | None, name: str) -> OutputSpec:
    sec = sec or _Section({}, "output")
    formats = tuple(f.lower() for f in (sec.text_list("formats") or ("csv", "xlsx")))
    bad = [f for f in formats if f not in OUTPUT_FORMATS]
    if bad:
        raise ConfigError(
            f"output.formats: неизвестные форматы {', '.join(bad)} "
            f"(допустимы: {', '.join(OUTPUT_FORMATS)}; Google Sheets — в output.google_sheets)"
        )
    bool_values = sec.text_list("bool_values") or ("да", "нет")
    if len(bool_values) != 2:
        raise ConfigError("output.bool_values: нужны ровно два значения, например [да, нет]")
    delimiter = sec.text("csv_delimiter", ";") or ";"
    if len(delimiter) != 1:
        raise ConfigError("output.csv_delimiter: нужен один символ, например ; или ,")
    gs = sec.section("google_sheets")
    google = GoogleSheetsSpec()
    if gs is not None:
        google = GoogleSheetsSpec(
            spreadsheet=gs.text("spreadsheet", "") or "",
            worksheet=gs.text("worksheet", "Данные") or "Данные",
            changes_worksheet=gs.text("changes_worksheet", "Изменения") or None,
        )
        gs.finish()
    spec = OutputSpec(
        dir=sec.text("dir", f"output/{name}") or f"output/{name}",
        basename=sec.text("basename", name) or name,
        formats=formats,
        sheet_name=(sec.text("sheet_name", "Данные") or "Данные")[:31],
        csv_delimiter=delimiter,
        list_separator=sec.text("list_separator", ", ") or ", ",
        bool_values=(bool_values[0], bool_values[1]),
        url_label=sec.text("url_label", "Ссылка") or "Ссылка",
        google_sheets=google,
    )
    sec.finish()
    return spec


def _read_urls_file(path_text: str, base: Path) -> tuple[str, ...]:
    path = Path(path_text)
    if not path.is_absolute():
        path = base / path
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ConfigError(f"start_urls_file: не удалось прочитать {path}: {exc}") from None
    return tuple(line.strip() for line in lines if line.strip() and not line.startswith("#"))


def _interpolate(value: Any, path: str = "") -> Any:
    """Подставить переменные окружения ``${VAR}`` / ``${VAR:-default}`` во все строки."""
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            var, default = match.group(1), match.group(2)
            env_value = os.environ.get(var)
            if env_value is not None and env_value != "":
                return env_value
            if default is not None:
                return default
            raise ConfigError(f"{path or 'конфиг'}: переменная окружения {var} не задана")

        return _ENV_RE.sub(repl, value)
    if isinstance(value, Mapping):
        return {k: _interpolate(v, f"{path}.{k}" if path else str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


def parse_config(data: Any, *, source: Path | None = None, base_dir: Path | None = None) -> SiteConfig:
    """Собрать ``SiteConfig`` из уже прочитанного YAML (словаря)."""
    root = _Section(_interpolate(data), "")
    name = root.req_text("name")
    if not _NAME_RE.match(name):
        raise ConfigError("name: только латиница, цифры, «-» и «_» (идёт в имена файлов)")
    title = root.text("title", name) or name

    start_urls = list(root.text_list("start_urls"))
    urls_file = root.text("start_urls_file")
    if urls_file:
        start_urls.extend(_read_urls_file(urls_file, base_dir or Path.cwd()))
    for url in start_urls:
        if not url.startswith(("http://", "https://")):
            raise ConfigError(f"start_urls: адрес должен начинаться с http:// или https://: {url!r}")

    list_spec: ListSpec | None = None
    if (ls := root.section("list")) is not None:
        link = ls.text("link")
        list_spec = ListSpec(
            item=_check_css(ls.req_text("item"), "list.item"),
            link=_check_css(link, "list.link") if link else None,
            fields=_parse_fields(ls),
        )
        ls.finish()

    detail_spec: DetailSpec | None = None
    if (ds := root.section("detail")) is not None:
        specs_sec = ds.section("specs")
        detail_spec = DetailSpec(
            fields=_parse_fields(ds),
            specs=_parse_specs(specs_sec) if specs_sec else None,
        )
        ds.finish()
        if not detail_spec.fields and not detail_spec.specs:
            raise ConfigError("detail: нужно хотя бы одно поле или блок specs")

    if list_spec and detail_spec and not list_spec.link:
        raise ConfigError("list.link: задайте селектор ссылки на карточку, раз есть секция detail")
    if list_spec is None and detail_spec is None:
        raise ConfigError("нужна секция list (каталог) и/или detail (карточки по ссылкам)")
    if list_spec and not list_spec.fields and not detail_spec:
        raise ConfigError("list.fields: не задано ни одного поля")

    pagination: PaginationSpec | None = None
    if (ps := root.section("pagination")) is not None:
        next_sel, template = ps.text("next"), ps.text("template")
        if next_sel and template:
            raise ConfigError("pagination: задайте что-то одно — next (ссылка) или template (шаблон)")
        if next_sel:
            _check_css(next_sel, "pagination.next")
        if template and "{page}" not in template:
            raise ConfigError("pagination.template: в шаблоне нужен {page}, например /catalog?page={page}")
        pagination = PaginationSpec(
            next=next_sel,
            template=template,
            start=ps.integer("start", 1),
            max_pages=ps.opt_integer("max_pages"),
        )
        ps.finish()
        if list_spec is None:
            raise ConfigError("pagination: пагинация работает только вместе с секцией list")
    if not start_urls:
        if pagination and pagination.template:
            start_urls = [pagination.template.format(page=pagination.start)]
        else:
            raise ConfigError("start_urls: не задано ни одного адреса")

    images: ImagesSpec | None = None
    if (im := root.section("images")) is not None:
        images = ImagesSpec(
            field=im.req_text("field"),
            dir=im.text("dir", "images") or "images",
            max=im.opt_integer("max", minimum=0),
            column=im.text("column", "image_file") or "image_file",
            label=im.text("label", "Файл картинки") or "Файл картинки",
        )
        im.finish()

    ch = root.section("changes") or _Section({}, "changes")
    changes = ChangesSpec(
        key=ch.text_list("key") or (URL_COLUMN,),
        title_field=ch.text("title_field"),
        price_field=ch.text("price_field"),
        track=ch.text_list("track"),
        gone_guard_ratio=ch.number("gone_guard_ratio", 0.5),
    )
    ch.finish()

    telegram = TelegramSpec()
    if (ns := root.section("notify")) is not None:
        if (tg := ns.section("telegram")) is not None:
            telegram = TelegramSpec(
                enabled=tg.flag("enabled", True),
                only_changes=tg.flag("only_changes", True),
                max_lines=tg.integer("max_lines", 10, minimum=1),
            )
            tg.finish()
        ns.finish()

    st = root.section("storage") or _Section({}, "storage")
    storage = StorageSpec(
        path=st.text("path", "state/site_parser.sqlite3") or "state/site_parser.sqlite3",
        keep_runs=st.integer("keep_runs", 30, minimum=2),
    )
    st.finish()

    config = SiteConfig(
        name=name,
        title=title,
        start_urls=tuple(start_urls),
        request=_parse_request(root.section("request")),
        output=_parse_output(root.section("output"), name),
        changes=changes,
        telegram=telegram,
        storage=storage,
        listing=list_spec,
        detail=detail_spec,
        pagination=pagination,
        images=images,
        max_items=root.opt_integer("max_items"),
        source=source,
    )
    root.finish()
    _check_references(config)
    return config


def _check_references(config: SiteConfig) -> None:
    """Ключ, трекинг и картинки должны ссылаться на существующие поля."""
    known = {spec.name for spec in config.all_fields()} | {URL_COLUMN}
    specs_prefix = config.detail.specs.prefix if config.detail and config.detail.specs else None
    for key in config.changes.key:
        if key not in known:
            raise ConfigError(f"changes.key: поля {key!r} нет среди полей конфига")
    for name in config.changes.track:
        if name not in known and not (specs_prefix is not None and name.startswith(specs_prefix)):
            raise ConfigError(f"changes.track: поля {name!r} нет среди полей конфига")
    for opt, value in (
        ("changes.title_field", config.changes.title_field),
        ("changes.price_field", config.changes.price_field),
    ):
        if value and value not in known:
            raise ConfigError(f"{opt}: поля {value!r} нет среди полей конфига")
    if config.images and config.images.field not in known:
        raise ConfigError(f"images.field: поля {config.images.field!r} нет среди полей конфига")


def load_config(path: str | Path) -> SiteConfig:
    """Прочитать и проверить YAML-конфиг."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"не удалось прочитать конфиг {path}: {exc.strerror or exc}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: ошибка синтаксиса YAML: {exc}") from None
    if data is None:
        raise ConfigError(f"{path}: файл пустой")
    try:
        return parse_config(data, source=path, base_dir=path.parent)
    except ConfigError as exc:
        raise ConfigError(f"{path.name}: {exc}") from None
