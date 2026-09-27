"""Секреты и пути из окружения (``.env``).

``.env`` ищется только в папке проекта парсера (или по явному пути
``--env-file``) — вверх по дереву каталогов поиск не идёт, чтобы случайно не
подхватить чужой ``.env`` из родительской папки.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from site_parser.redact import register_secrets

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


@dataclass(frozen=True)
class EnvSettings:
    telegram_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = None
    google_credentials_file: Path | None = None

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> EnvSettings:
        env = os.environ if environ is None else environ
        token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip() or None
        chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip() or None
        creds = (env.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
        creds_path: Path | None = None
        if creds:
            creds_path = Path(creds).expanduser()
            if not creds_path.is_absolute():
                creds_path = PROJECT_ROOT / creds_path
        register_secrets([token])
        return cls(telegram_token=token, telegram_chat_id=chat_id, google_credentials_file=creds_path)


def load_env(env_file: str | Path | None = None) -> Path | None:
    """Загрузить переменные из ``.env`` (уже заданные в окружении не перетираются).

    Возвращает путь к прочитанному файлу или ``None``, если файла нет.
    Явно указанный, но отсутствующий файл — ошибка.
    """
    path = Path(env_file) if env_file else DEFAULT_ENV_FILE
    if not path.is_file():
        if env_file:
            raise FileNotFoundError(f"файл окружения не найден: {path}")
        return None
    load_dotenv(path, override=False)
    return path
