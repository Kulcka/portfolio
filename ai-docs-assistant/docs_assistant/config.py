"""Настройки из переменных окружения и файла ``.env``.

Все параметры описаны в ``.env.example``. Значения секретов оборачиваются
в :class:`~docs_assistant.security.Secret` и регистрируются для маскировки
в логах сразу при чтении. Тексты ошибок называют переменную, но никогда
не показывают её значение.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from docs_assistant.security import REGISTRY, Secret

PROVIDERS = ("extractive", "fake", "openai", "yandexgpt", "gigachat")

DEFAULT_GIGACHAT_BASE_URL = "https://api.giga.chat/v1"
DEFAULT_GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
DEFAULT_YANDEX_NATIVE_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
DEFAULT_YANDEX_OPENAI_BASE_URL = "https://ai.api.cloud.yandex.net/v1"

# Порог полноты совпадения запроса (см. retriever.py). Подобран командой
# `python -m docs_assistant eval --tune` на dev-части вопросов демо-корпуса;
# для документов заказчика порог нужно перемерить на его вопросах.
DEFAULT_MIN_COVERAGE = 0.35


class ConfigError(ValueError):
    """Ошибка конфигурации. Текст безопасен для вывода: без значений секретов."""


@dataclass(frozen=True)
class OpenAISettings:
    base_url: str = "https://api.openai.com/v1"
    api_key: Secret = field(default_factory=lambda: Secret(""))
    model: str = "gpt-4o-mini"


@dataclass(frozen=True)
class YandexSettings:
    api_key: Secret = field(default_factory=lambda: Secret(""))
    iam_token: Secret = field(default_factory=lambda: Secret(""))
    folder_id: str = ""
    model: str = "yandexgpt-lite"
    api_mode: str = "native"  # native | openai
    native_url: str = DEFAULT_YANDEX_NATIVE_URL
    openai_base_url: str = DEFAULT_YANDEX_OPENAI_BASE_URL
    data_logging: bool = False


@dataclass(frozen=True)
class GigaChatSettings:
    auth_key: Secret = field(default_factory=lambda: Secret(""))
    scope: str = "GIGACHAT_API_PERS"
    model: str = "GigaChat-2"
    base_url: str = DEFAULT_GIGACHAT_BASE_URL
    oauth_url: str = DEFAULT_GIGACHAT_OAUTH_URL
    ca_bundle: Path | None = None


@dataclass(frozen=True)
class EmbeddingsSettings:
    base_url: str
    model: str
    api_key: Secret = field(default_factory=lambda: Secret(""))
    query_model: str | None = None
    project: str | None = None
    batch_size: int = 16
    min_similarity: float | None = None


@dataclass(frozen=True)
class Settings:
    # Документы и индекс
    docs_dir: Path = Path("demo_docs")
    index_dir: Path = Path("data/index")
    chunk_size: int = 800
    chunk_overlap: int = 150
    max_file_mb: float = 50.0
    # Поиск
    top_k: int = 4
    min_coverage: float = DEFAULT_MIN_COVERAGE
    synonyms_file: Path | None = None
    embeddings: EmbeddingsSettings | None = None
    # Ответ
    company_name: str = "ООО «Ромашка-Логистик»"
    manager_contact: str = "менеджером по телефону +7 (000) 000-00-00 или support@romashka-logistic.example"
    require_citations: bool = True
    max_question_chars: int = 1000
    # Модель
    llm_provider: str = "extractive"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 700
    llm_timeout: float = 60.0
    openai: OpenAISettings = field(default_factory=OpenAISettings)
    yandex: YandexSettings = field(default_factory=YandexSettings)
    gigachat: GigaChatSettings = field(default_factory=GigaChatSettings)
    # Telegram
    telegram_token: Secret = field(default_factory=lambda: Secret(""))
    telegram_admin_ids: frozenset[int] = frozenset()
    telegram_rate_limit_seconds: float = 3.0
    # Журнал
    log_questions: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        base_dir: Path | None = None,
        dotenv_path: Path | None = None,
    ) -> Settings:
        """Прочитать настройки.

        :param env: словарь переменных (по умолчанию ``os.environ`` + ``.env``).
        :param base_dir: от какой папки считать относительные пути (по умолчанию — текущая).
        :param dotenv_path: путь к ``.env``; по умолчанию ``base_dir/.env``, если он есть.
        """
        base = (base_dir or Path.cwd()).resolve()
        if env is None:
            env = _environment_with_dotenv(dotenv_path or base / ".env")
        reader = _EnvReader(env)

        embeddings = None
        emb_model = reader.str("EMBEDDINGS_MODEL")
        if emb_model:
            embeddings = EmbeddingsSettings(
                base_url=reader.str("EMBEDDINGS_BASE_URL") or reader.str("OPENAI_BASE_URL")
                or OpenAISettings.base_url,
                model=emb_model,
                api_key=reader.secret("EMBEDDINGS_API_KEY") or reader.secret("OPENAI_API_KEY"),
                query_model=reader.str("EMBEDDINGS_QUERY_MODEL") or None,
                project=reader.str("EMBEDDINGS_PROJECT") or None,
                batch_size=reader.int("EMBEDDINGS_BATCH_SIZE", 16, minimum=1),
                min_similarity=reader.optional_float("EMBEDDINGS_MIN_SIMILARITY"),
            )

        provider = (reader.str("LLM_PROVIDER") or "extractive").lower()
        if provider not in PROVIDERS:
            raise ConfigError(
                f"LLM_PROVIDER: неизвестный провайдер «{provider}». Допустимо: {', '.join(PROVIDERS)}"
            )

        yandex_mode = (reader.str("YANDEX_API_MODE") or "native").lower()
        if yandex_mode not in ("native", "openai"):
            raise ConfigError("YANDEX_API_MODE: допустимо native или openai")

        chunk_size = reader.int("CHUNK_SIZE", 800, minimum=200)
        chunk_overlap = reader.int("CHUNK_OVERLAP", 150, minimum=0)
        if chunk_overlap >= chunk_size // 2:
            raise ConfigError("CHUNK_OVERLAP должен быть меньше половины CHUNK_SIZE")

        ca_bundle = reader.str("GIGACHAT_CA_BUNDLE")
        synonyms = reader.str("SYNONYMS_FILE")

        return cls(
            docs_dir=_path(base, reader.str("DOCS_DIR") or "demo_docs"),
            index_dir=_path(base, reader.str("INDEX_DIR") or "data/index"),
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            max_file_mb=reader.float("MAX_FILE_MB", 50.0, minimum=0.1),
            top_k=reader.int("TOP_K", 4, minimum=1),
            min_coverage=reader.float("MIN_COVERAGE", DEFAULT_MIN_COVERAGE, minimum=0.0, maximum=1.0),
            synonyms_file=_path(base, synonyms) if synonyms else None,
            embeddings=embeddings,
            company_name=reader.str("COMPANY_NAME") or cls.company_name,
            manager_contact=reader.str("MANAGER_CONTACT") or cls.manager_contact,
            require_citations=reader.bool("REQUIRE_CITATIONS", True),
            max_question_chars=reader.int("MAX_QUESTION_CHARS", 1000, minimum=50),
            llm_provider=provider,
            llm_temperature=reader.float("LLM_TEMPERATURE", 0.1, minimum=0.0, maximum=2.0),
            llm_max_tokens=reader.int("LLM_MAX_TOKENS", 700, minimum=16),
            llm_timeout=reader.float("LLM_TIMEOUT", 60.0, minimum=1.0),
            openai=OpenAISettings(
                base_url=reader.str("OPENAI_BASE_URL") or OpenAISettings.base_url,
                api_key=reader.secret("OPENAI_API_KEY"),
                model=reader.str("OPENAI_MODEL") or OpenAISettings.model,
            ),
            yandex=YandexSettings(
                api_key=reader.secret("YANDEX_API_KEY"),
                iam_token=reader.secret("YANDEX_IAM_TOKEN"),
                folder_id=reader.str("YANDEX_FOLDER_ID"),
                model=reader.str("YANDEX_MODEL") or YandexSettings.model,
                api_mode=yandex_mode,
                native_url=reader.str("YANDEX_NATIVE_URL") or DEFAULT_YANDEX_NATIVE_URL,
                openai_base_url=reader.str("YANDEX_OPENAI_BASE_URL") or DEFAULT_YANDEX_OPENAI_BASE_URL,
                data_logging=reader.bool("YANDEX_DATA_LOGGING", False),
            ),
            gigachat=GigaChatSettings(
                auth_key=reader.secret("GIGACHAT_AUTH_KEY"),
                scope=reader.str("GIGACHAT_SCOPE") or GigaChatSettings.scope,
                model=reader.str("GIGACHAT_MODEL") or GigaChatSettings.model,
                base_url=reader.str("GIGACHAT_BASE_URL") or DEFAULT_GIGACHAT_BASE_URL,
                oauth_url=reader.str("GIGACHAT_OAUTH_URL") or DEFAULT_GIGACHAT_OAUTH_URL,
                ca_bundle=_path(base, ca_bundle) if ca_bundle else None,
            ),
            telegram_token=reader.secret("TELEGRAM_BOT_TOKEN"),
            telegram_admin_ids=reader.int_set("TELEGRAM_ADMIN_IDS"),
            telegram_rate_limit_seconds=reader.float("TELEGRAM_RATE_LIMIT_SECONDS", 3.0, minimum=0.0),
            log_questions=reader.bool("LOG_QUESTIONS", False),
            log_level=(reader.str("LOG_LEVEL") or "INFO").upper(),
        )


def _environment_with_dotenv(dotenv_path: Path) -> dict[str, str]:
    """os.environ поверх значений из .env (переменные окружения важнее файла)."""
    values: dict[str, str] = {}
    if dotenv_path.is_file():
        from dotenv import dotenv_values

        values.update({k: v for k, v in dotenv_values(dotenv_path).items() if v is not None})
    values.update(os.environ)
    return values


def _path(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path)


class _EnvReader:
    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env

    def str(self, name: str) -> str:
        return (self._env.get(name) or "").strip()

    def secret(self, name: str) -> Secret:
        secret = Secret(self.str(name))
        REGISTRY.add(secret)
        return secret

    def int(self, name: str, default: int, *, minimum: int | None = None) -> int:
        raw = self.str(name)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise ConfigError(f"{name}: ожидается целое число") from None
        if minimum is not None and value < minimum:
            raise ConfigError(f"{name}: значение должно быть не меньше {minimum}")
        return value

    def float(
        self, name: str, default: float, *, minimum: float | None = None, maximum: float | None = None
    ) -> float:
        raw = self.str(name)
        if not raw:
            return default
        value = self._parse_float(name, raw)
        if minimum is not None and value < minimum:
            raise ConfigError(f"{name}: значение должно быть не меньше {minimum}")
        if maximum is not None and value > maximum:
            raise ConfigError(f"{name}: значение должно быть не больше {maximum}")
        return value

    def optional_float(self, name: str) -> float | None:
        raw = self.str(name)
        return self._parse_float(name, raw) if raw else None

    @staticmethod
    def _parse_float(name: str, raw: str) -> float:
        try:
            return float(raw.replace(",", "."))
        except ValueError:
            raise ConfigError(f"{name}: ожидается число") from None

    def bool(self, name: str, default: bool) -> bool:
        raw = self.str(name).lower()
        if not raw:
            return default
        if raw in ("1", "true", "yes", "on", "да"):
            return True
        if raw in ("0", "false", "no", "off", "нет"):
            return False
        raise ConfigError(f"{name}: ожидается true или false")

    def int_set(self, name: str) -> frozenset[int]:
        raw = self.str(name)
        if not raw:
            return frozenset()
        result: set[int] = set()
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                result.add(int(part))
            except ValueError:
                raise ConfigError(f"{name}: ожидается список числовых id через запятую") from None
        return frozenset(result)
