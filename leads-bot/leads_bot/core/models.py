"""Модели ядра: пользователь, ответы бота, диалог, заявка.

Ответ бота (`Reply`) описан без привязки к мессенджеру: текст в HTML и
клавиатура из кнопок трёх видов. Транспорт сам решает, как их отрисовать
(в Telegram кнопка «поделиться номером» живёт в нижней клавиатуре, в MAX —
в инлайн-клавиатуре).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Channel(str, Enum):
    TELEGRAM = "telegram"
    MAX = "max"
    CONSOLE = "console"  # демо-режим в терминале, без мессенджера

    @property
    def title(self) -> str:
        return {"telegram": "Telegram", "max": "MAX", "console": "Консоль (демо)"}[self.value]


@dataclass(frozen=True)
class UserRef:
    """Кто пишет боту. `user_id` уникален в пределах канала."""

    channel: Channel
    user_id: int
    username: str | None = None
    display_name: str | None = None


class ButtonKind(Enum):
    CALLBACK = "callback"
    URL = "url"
    REQUEST_CONTACT = "request_contact"


@dataclass(frozen=True)
class Button:
    text: str
    kind: ButtonKind
    value: str = ""  # payload для CALLBACK, адрес для URL

    @classmethod
    def callback(cls, text: str, payload: str) -> Button:
        return cls(text=text, kind=ButtonKind.CALLBACK, value=payload)

    @classmethod
    def url(cls, text: str, url: str) -> Button:
        return cls(text=text, kind=ButtonKind.URL, value=url)

    @classmethod
    def request_contact(cls, text: str) -> Button:
        return cls(text=text, kind=ButtonKind.REQUEST_CONTACT)


Keyboard = tuple[tuple[Button, ...], ...]


@dataclass(frozen=True)
class Reply:
    """Одно сообщение бота.

    `clear_keyboard` — просьба убрать постоянную клавиатуру, показанную раньше
    (в Telegram это кнопка «Отправить номер»). Транспорты без такой клавиатуры
    флаг игнорируют.
    """

    text: str
    keyboard: Keyboard = ()
    clear_keyboard: bool = False

    @property
    def buttons(self) -> list[Button]:
        return [button for row in self.keyboard for button in row]


class Step(str, Enum):
    CONSENT = "consent"
    SERVICE = "service"
    NAME = "name"
    PHONE = "phone"
    COMMENT = "comment"
    CONFIRM = "confirm"


@dataclass
class Dialog:
    """Незаконченная заявка. Хранится до отправки, отмены или истечения срока."""

    channel: Channel
    user_id: int
    step: Step
    updated_at: datetime
    data: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LeadDraft:
    created_at: datetime
    channel: Channel
    user_id: int
    username: str | None
    display_name: str | None
    service_id: str
    service_title: str
    name: str
    phone: str
    comment: str
    consent_at: datetime
    policy_url: str
    policy_version: str


@dataclass(frozen=True)
class Lead(LeadDraft):
    id: int = 0
    admin_notified: bool = False


@dataclass(frozen=True)
class ExportFile:
    filename: str
    content: bytes
    count: int
