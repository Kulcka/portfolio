from __future__ import annotations

import logging
from pathlib import Path

import pytest

from docs_assistant.config import ConfigError, Settings
from docs_assistant.security import REGISTRY, RedactingFilter, Secret, redact, safe_url

BOT_TOKEN = "123456789:AAHfakeTokenForTests_abcdefghijklmno"


def test_secret_repr_hides_value() -> None:
    secret = Secret("very-secret-value")
    assert "very-secret" not in repr(secret) and "very-secret" not in str(secret)
    assert "very-secret" not in f"{secret!r} {secret}"
    assert secret.get() == "very-secret-value" and not Secret("")


def test_redact_registered_and_known_formats() -> None:
    REGISTRY.add("my-registered-secret-42")
    text = (
        f"token {BOT_TOKEN} url https://api.telegram.org/bot{BOT_TOKEN}/getMe "
        "Authorization: Bearer abcdefghijklmnop Api-Key AQVNxxxxxxxxxxxx "
        "key sk-proj-1234567890abcdefXYZ and my-registered-secret-42"
    )
    cleaned = redact(text)
    for leaked in (BOT_TOKEN, "abcdefghijklmnop", "AQVNxxxxxxxxxxxx", "sk-proj-1234567890abcdefXYZ",
                   "my-registered-secret-42"):
        assert leaked not in cleaned
    assert "Bearer ***" in cleaned and "Api-Key ***" in cleaned


def test_log_filter_masks_message_args_and_traceback() -> None:
    REGISTRY.add("log-secret-value-777")
    logger = logging.getLogger("test.redact")
    records: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    handler = Collect()
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    try:
        logger.error("ключ %s", "log-secret-value-777")
        try:
            raise RuntimeError(f"failed with {BOT_TOKEN}")
        except RuntimeError:
            logger.exception("ошибка")
    finally:
        logger.removeHandler(handler)
    joined = "\n".join(records)
    assert "log-secret-value-777" not in joined and BOT_TOKEN not in joined
    assert "RuntimeError" in joined  # трассировка осталась, только без секрета


def test_safe_url() -> None:
    assert safe_url("https://u:p@host.example:8443/v1/x?key=1#f") == "https://host.example:8443/v1/x"


def test_settings_defaults_and_parsing(tmp_path: Path) -> None:
    settings = Settings.from_env(
        {
            "LLM_PROVIDER": "GigaChat",
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "TELEGRAM_ADMIN_IDS": "111, 222;333",
            "DOCS_DIR": "my_docs",
            "MIN_COVERAGE": "0,4",
            "YANDEX_DATA_LOGGING": "нет",
        },
        base_dir=tmp_path,
    )
    assert settings.llm_provider == "gigachat"
    assert settings.telegram_admin_ids == frozenset({111, 222, 333})
    assert settings.docs_dir == tmp_path / "my_docs"
    assert settings.min_coverage == 0.4
    assert BOT_TOKEN not in repr(settings)
    assert redact(f"x {BOT_TOKEN}") == "x ***"


def test_settings_errors_do_not_echo_values() -> None:
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        Settings.from_env({"LLM_PROVIDER": "claude-magic"})
    with pytest.raises(ConfigError) as info:
        Settings.from_env({"TELEGRAM_ADMIN_IDS": "secret-looking-value"})
    assert "secret-looking-value" not in str(info.value)
    with pytest.raises(ConfigError, match="CHUNK_OVERLAP"):
        Settings.from_env({"CHUNK_SIZE": "300", "CHUNK_OVERLAP": "200"})


def test_dotenv_file_is_read_and_environment_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_PROVIDER=openai\nOPENAI_MODEL=from-file\nTOP_K=7\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_MODEL", "from-env")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("TOP_K", raising=False)
    settings = Settings.from_env(base_dir=tmp_path)
    assert settings.llm_provider == "openai" and settings.top_k == 7
    assert settings.openai.model == "from-env"
