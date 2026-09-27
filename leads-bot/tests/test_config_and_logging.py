"""Настройки и логирование без секретов."""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pytest
import yaml

from leads_bot.config import ConfigError, load_bot_config, load_settings
from leads_bot.logging_setup import SecretMaskingFormatter, mask_secrets
from tests.helpers import CONFIG_PATH

FAKE_TG_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijkl"
FAKE_MAX_TOKEN = "max-fake-token-for-tests-0123456789"


def write_config(tmp_path: Path, mutate) -> Path:  # type: ignore[no-untyped-def]
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_project_config_is_valid() -> None:
    config = load_bot_config(CONFIG_PATH)
    assert len(config.services) == 6
    assert config.service_by_id("design").title == "Дизайн-проект"  # type: ignore[union-attr]
    assert config.tz.key == "Europe/Moscow"


def test_unknown_placeholder_is_reported(tmp_path: Path) -> None:
    path = write_config(tmp_path, lambda d: d["texts"].update(lead_accepted="Спасибо, {nmae}!"))
    with pytest.raises(ConfigError, match=r"texts\.lead_accepted: неизвестная подстановка \{nmae\}"):
        load_bot_config(path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["services"].append({"id": "design", "title": "Ещё дизайн"}), "повторяются id услуг: design"),
        (lambda d: d["services"].append({"id": "Bad Id", "title": "x"}), "services"),
        (lambda d: d.update(timezone="Mars/Olympus"), "неизвестный часовой пояс"),
        (lambda d: d["privacy"].update(policy_url="example.com/privacy"), "https://"),
        (lambda d: d["texts"].update(send_button="О" * 65), "текст кнопки длиннее 64"),
        (lambda d: d["texts"].pop("confirm"), "texts.confirm"),
        (lambda d: d["antispam"].update(max_leads_per_hour=0), "max_leads_per_hour"),
        (lambda d: d.update(unknown_key=1), "unknown_key"),
    ],
)
def test_invalid_config(tmp_path: Path, mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConfigError, match=message):
        load_bot_config(write_config(tmp_path, mutate))


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Не найден файл"):
        load_bot_config(tmp_path / "nope.yaml")


def test_settings_from_env() -> None:
    settings = load_settings(
        {
            "TELEGRAM_BOT_TOKEN": FAKE_TG_TOKEN,
            "TELEGRAM_ADMIN_CHAT_ID": "-1001234567890",
            "TELEGRAM_ADMIN_USER_IDS": "11, 22;33",
            "MAX_BOT_TOKEN": FAKE_MAX_TOKEN,
            "MAX_ADMIN_CHAT_ID": "-777",
            "DATABASE_PATH": "data/test.sqlite3",
        }
    )
    assert settings.telegram_admin_chat_id == -1001234567890
    assert settings.telegram_admin_user_ids == frozenset({11, 22, 33})
    assert settings.max_admin_chat_id == -777 and settings.max_mode == "polling"
    assert settings.max_api_url == "https://platform-api2.max.ru"
    assert set(settings.secrets) == {FAKE_TG_TOKEN, FAKE_MAX_TOKEN}
    assert FAKE_TG_TOKEN not in repr(settings) and FAKE_MAX_TOKEN not in repr(settings)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"TELEGRAM_ADMIN_CHAT_ID": "мой чат"}, "TELEGRAM_ADMIN_CHAT_ID должен быть целым"),
        ({"MAX_ADMIN_USER_IDS": "1,два"}, "MAX_ADMIN_USER_IDS"),
        ({"MAX_MODE": "push"}, "MAX_MODE"),
        ({"MAX_MODE": "webhook"}, "MAX_WEBHOOK_URL"),
        ({"MAX_MODE": "webhook", "MAX_WEBHOOK_URL": "http://bot.example.ru/max"}, "https://"),
        ({"MAX_MODE": "webhook", "MAX_WEBHOOK_URL": "https://bot.example.ru/max"}, "MAX_WEBHOOK_SECRET"),
        ({"MAX_CA_BUNDLE": "/no/such/file.pem"}, "MAX_CA_BUNDLE"),
        ({"LOG_LEVEL": "LOUD"}, "LOG_LEVEL"),
    ],
)
def test_invalid_settings(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_settings(env)


def test_webhook_settings() -> None:
    settings = load_settings(
        {"MAX_MODE": "webhook", "MAX_WEBHOOK_URL": "https://bot.example.ru/max/hook", "MAX_WEBHOOK_SECRET": "s3cret-value"}
    )
    assert settings.max_webhook_path == "/max/hook"
    assert "s3cret-value" in settings.secrets and "s3cret-value" not in repr(settings)


def test_mask_secrets() -> None:
    text = f"GET https://api.telegram.org/bot{FAKE_TG_TOKEN}/getMe failed; max={FAKE_MAX_TOKEN}"
    masked = mask_secrets(text, [FAKE_MAX_TOKEN])
    assert FAKE_TG_TOKEN not in masked and FAKE_MAX_TOKEN not in masked
    assert "/bot***/getMe" in masked
    # Токен Telegram маскируется по шаблону, даже если его нет в списке секретов.
    assert FAKE_TG_TOKEN not in mask_secrets(text)
    # Короткие «секреты» не превращают весь лог в звёздочки.
    assert mask_secrets("abc abc", ["abc"]) == "abc abc"


def test_formatter_masks_args_and_tracebacks() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(SecretMaskingFormatter("%(levelname)s %(message)s", [FAKE_MAX_TOKEN]))
    logger = logging.getLogger("test.masking")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.error("token=%s", FAKE_MAX_TOKEN)
        try:
            raise RuntimeError(f"request to /bot{FAKE_TG_TOKEN}/sendMessage failed, auth {FAKE_MAX_TOKEN}")
        except RuntimeError:
            logger.exception("request failed")
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert "RuntimeError" in output and "request failed" in output
    assert FAKE_MAX_TOKEN not in output and FAKE_TG_TOKEN not in output
