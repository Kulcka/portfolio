"""Настройки бота: `config.yaml` (услуги, тексты, политика) и `.env` (секреты).

Всё, что видит клиент, лежит в `config.yaml` и проверяется при старте: если в
тексте опечатка в подстановке или повторяется id услуги, бот не запустится и
скажет, что именно не так. Токены и id чатов — только в переменных окружения
(`.env`), в репозиторий они не попадают.
"""

from __future__ import annotations

import os
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigError(Exception):
    """Ошибка в настройках. Текст годится для показа человеку и не содержит секретов."""


# --- config.yaml --------------------------------------------------------------------------

SERVICE_ID_PATTERN = r"^[a-z0-9_-]{1,32}$"
BUTTON_TEXT_MAX = 64

# Какие подстановки допустимы в каждом тексте. Проверяется при загрузке конфига,
# чтобы опечатка вида {phnoe} не всплыла у клиента посреди заявки.
TEXT_PLACEHOLDERS: dict[str, frozenset[str]] = {
    "consent": frozenset({"company", "policy_url"}),
    "policy_button": frozenset(),
    "consent_accept_button": frozenset(),
    "consent_decline_button": frozenset(),
    "consent_declined": frozenset({"company", "company_phone"}),
    "consent_required": frozenset(),
    "choose_service": frozenset(),
    "ask_name": frozenset(),
    "invalid_name": frozenset({"min_length", "max_length"}),
    "ask_phone": frozenset(),
    "share_contact_button": frozenset(),
    "invalid_phone": frozenset(),
    "phone_saved": frozenset({"phone"}),
    "ask_comment": frozenset(),
    "skip_button": frozenset(),
    "comment_too_long": frozenset({"max_length"}),
    "no_comment": frozenset(),
    "confirm": frozenset({"service", "name", "phone", "comment"}),
    "send_button": frozenset(),
    "edit_button": frozenset(),
    "cancel_button": frozenset(),
    "lead_accepted": frozenset({"name", "lead_id"}),
    "rate_limited": frozenset({"count", "minutes", "company_phone"}),
    "cancelled": frozenset(),
    "idle_hint": frozenset({"company"}),
    "start_button": frozenset(),
    "use_buttons": frozenset(),
    "stale_button": frozenset(),
    "data_deleted": frozenset({"count"}),
    "callback_ack": frozenset(),
}

BUTTON_TEXT_KEYS = frozenset(key for key in TEXT_PLACEHOLDERS if key.endswith("_button"))


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Service(_Strict):
    id: str = Field(pattern=SERVICE_ID_PATTERN)
    title: str = Field(min_length=1, max_length=BUTTON_TEXT_MAX)


class Company(_Strict):
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(min_length=1, max_length=40)


class Privacy(_Strict):
    policy_url: str
    policy_version: str = ""

    @field_validator("policy_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("ссылка на политику должна начинаться с https:// (или http://)")
        return value


class Antispam(_Strict):
    max_leads_per_hour: int = Field(ge=1, le=100)


class Notifications(_Strict):
    mask_phone: bool = False


class Texts(_Strict):
    consent: str
    policy_button: str
    consent_accept_button: str
    consent_decline_button: str
    consent_declined: str
    consent_required: str
    choose_service: str
    ask_name: str
    invalid_name: str
    ask_phone: str
    share_contact_button: str
    invalid_phone: str
    phone_saved: str
    ask_comment: str
    skip_button: str
    comment_too_long: str
    no_comment: str
    confirm: str
    send_button: str
    edit_button: str
    cancel_button: str
    lead_accepted: str
    rate_limited: str
    cancelled: str
    idle_hint: str
    start_button: str
    use_buttons: str
    stale_button: str
    data_deleted: str
    callback_ack: str

    @model_validator(mode="after")
    def _check_templates(self) -> Texts:
        problems: list[str] = []
        for key, allowed in TEXT_PLACEHOLDERS.items():
            template: str = getattr(self, key)
            if not template.strip():
                problems.append(f"texts.{key}: пустой текст")
                continue
            try:
                used = {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}
            except ValueError as exc:
                problems.append(f"texts.{key}: ошибка в фигурных скобках ({exc})")
                continue
            unknown = sorted(name for name in used if name not in allowed)
            if unknown:
                hint = ", ".join(f"{{{n}}}" for n in sorted(allowed)) or "подстановок нет"
                problems.append(
                    f"texts.{key}: неизвестная подстановка {', '.join('{' + u + '}' for u in unknown)}; "
                    f"допустимо: {hint}"
                )
            if key in BUTTON_TEXT_KEYS and len(template) > BUTTON_TEXT_MAX:
                problems.append(f"texts.{key}: текст кнопки длиннее {BUTTON_TEXT_MAX} символов")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class BotConfig(_Strict):
    company: Company
    privacy: Privacy
    timezone: str = "Europe/Moscow"
    antispam: Antispam
    dialog_ttl_hours: int = Field(default=24, ge=1, le=24 * 30)
    retention_days: int = Field(default=0, ge=0)
    notifications: Notifications = Notifications()
    services: tuple[Service, ...] = Field(min_length=1, max_length=30)
    texts: Texts

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"неизвестный часовой пояс {value!r}") from exc
        return value

    @field_validator("services")
    @classmethod
    def _check_unique_services(cls, value: tuple[Service, ...]) -> tuple[Service, ...]:
        ids = [service.id for service in value]
        duplicates = sorted({sid for sid in ids if ids.count(sid) > 1})
        if duplicates:
            raise ValueError(f"повторяются id услуг: {', '.join(duplicates)}")
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def service_by_id(self, service_id: str) -> Service | None:
        return next((s for s in self.services if s.id == service_id), None)


def load_bot_config(path: Path | str) -> BotConfig:
    """Прочитать и проверить `config.yaml`."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Не найден файл настроек {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: ошибка разметки YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: ожидался словарь настроек верхнего уровня")
    try:
        return BotConfig.model_validate(raw)
    except ValidationError as exc:
        details = "\n".join(
            f"  - {'.'.join(str(p) for p in err['loc']) or '(корень)'}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"{path}: ошибки в настройках:\n{details}") from None


# --- .env ---------------------------------------------------------------------------------

MaxMode = Literal["polling", "webhook"]
DEFAULT_MAX_API_URL = "https://platform-api2.max.ru"


@dataclass(frozen=True)
class Settings:
    """Переменные окружения. Токены скрыты из `repr`, чтобы не попасть в логи."""

    telegram_token: str | None = field(default=None, repr=False)
    telegram_admin_chat_id: int | None = None
    telegram_admin_user_ids: frozenset[int] = frozenset()

    max_token: str | None = field(default=None, repr=False)
    max_admin_chat_id: int | None = None
    max_admin_user_ids: frozenset[int] = frozenset()
    max_mode: MaxMode = "polling"
    max_api_url: str = DEFAULT_MAX_API_URL
    max_ca_bundle: Path | None = None
    max_webhook_url: str | None = None
    max_webhook_secret: str | None = field(default=None, repr=False)
    max_webhook_host: str = "127.0.0.1"
    max_webhook_port: int = 8080

    database_path: Path = Path("data/leads.sqlite3")
    config_path: Path = Path("config.yaml")
    log_level: str = "INFO"

    @property
    def secrets(self) -> tuple[str, ...]:
        """Все секреты — для маскирования в логах."""
        return tuple(s for s in (self.telegram_token, self.max_token, self.max_webhook_secret) if s)

    @property
    def max_webhook_path(self) -> str:
        if not self.max_webhook_url:
            return "/max/webhook"
        return urlparse(self.max_webhook_url).path or "/"


def _opt_str(env: Mapping[str, str | None], name: str) -> str | None:
    value = (env.get(name) or "").strip()
    return value or None


def _opt_int(env: Mapping[str, str | None], name: str) -> int | None:
    value = _opt_str(env, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} должен быть целым числом (id чата или пользователя)") from None


def _int_set(env: Mapping[str, str | None], name: str) -> frozenset[int]:
    value = _opt_str(env, name)
    if value is None:
        return frozenset()
    try:
        return frozenset(int(part) for part in value.replace(";", ",").split(",") if part.strip())
    except ValueError:
        raise ConfigError(f"{name}: ожидаются целые id через запятую") from None


def load_settings(env: Mapping[str, str | None] | None = None, *, env_file: Path | str | None = ".env") -> Settings:
    """Собрать настройки из окружения; `.env` дополняет, но не перекрывает переменные окружения."""
    if env is None:
        merged: dict[str, str | None] = {}
        if env_file is not None and Path(env_file).is_file():
            merged.update(dotenv_values(env_file))
        merged.update(os.environ)
        env = merged

    max_mode = (_opt_str(env, "MAX_MODE") or "polling").lower()
    if max_mode not in ("polling", "webhook"):
        raise ConfigError("MAX_MODE должен быть polling или webhook")

    webhook_url = _opt_str(env, "MAX_WEBHOOK_URL")
    webhook_secret = _opt_str(env, "MAX_WEBHOOK_SECRET")
    if max_mode == "webhook":
        if not webhook_url or not webhook_url.startswith("https://"):
            raise ConfigError("Для MAX_MODE=webhook нужен MAX_WEBHOOK_URL, начинающийся с https://")
        # Без секрета любой, кто узнал адрес, сможет слать боту поддельные события.
        if webhook_secret is None or not 5 <= len(webhook_secret) <= 256:
            raise ConfigError("Для MAX_MODE=webhook нужен MAX_WEBHOOK_SECRET длиной от 5 до 256 символов")

    ca_bundle = _opt_str(env, "MAX_CA_BUNDLE")
    if ca_bundle is not None and not Path(ca_bundle).is_file():
        raise ConfigError(f"MAX_CA_BUNDLE: файл {ca_bundle} не найден")

    port = _opt_int(env, "MAX_WEBHOOK_PORT")
    log_level = (_opt_str(env, "LOG_LEVEL") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("LOG_LEVEL: одно из DEBUG, INFO, WARNING, ERROR")

    return Settings(
        telegram_token=_opt_str(env, "TELEGRAM_BOT_TOKEN"),
        telegram_admin_chat_id=_opt_int(env, "TELEGRAM_ADMIN_CHAT_ID"),
        telegram_admin_user_ids=_int_set(env, "TELEGRAM_ADMIN_USER_IDS"),
        max_token=_opt_str(env, "MAX_BOT_TOKEN"),
        max_admin_chat_id=_opt_int(env, "MAX_ADMIN_CHAT_ID"),
        max_admin_user_ids=_int_set(env, "MAX_ADMIN_USER_IDS"),
        max_mode=max_mode,  # type: ignore[arg-type]
        max_api_url=(_opt_str(env, "MAX_API_URL") or DEFAULT_MAX_API_URL).rstrip("/"),
        max_ca_bundle=Path(ca_bundle) if ca_bundle else None,
        max_webhook_url=webhook_url,
        max_webhook_secret=webhook_secret,
        max_webhook_host=_opt_str(env, "MAX_WEBHOOK_HOST") or "127.0.0.1",
        max_webhook_port=port if port is not None else 8080,
        database_path=Path(_opt_str(env, "DATABASE_PATH") or "data/leads.sqlite3"),
        config_path=Path(_opt_str(env, "CONFIG_PATH") or "config.yaml"),
        log_level=log_level,
    )
