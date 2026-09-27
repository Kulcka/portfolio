"""Транспорт Telegram на aiogram 3.

Сценарий заявки работает только в личных сообщениях. В админ-чате (группа или
личка администратора) доступны /stats и /export; /id подсказывает id чата при
настройке.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart, Filter
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User,
)

from ..core.models import Button, ButtonKind, Channel, Reply, UserRef
from ..core.service import LeadService
from .common import ADMIN_EXPORT_CAPTION, ADMIN_NO_LEADS, CHAT_ID_REPLY, AdminAccess

log = logging.getLogger(__name__)

TelegramMarkup = InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None

PRIVATE_COMMANDS = (
    BotCommand(command="start", description="Оставить заявку"),
    BotCommand(command="cancel", description="Отменить заявку"),
    BotCommand(command="forget", description="Удалить мои данные"),
)
ADMIN_COMMANDS = (
    BotCommand(command="stats", description="Статистика заявок"),
    BotCommand(command="export", description="Выгрузка в Excel"),
    BotCommand(command="id", description="Показать id чата"),
)


def user_ref(user: User) -> UserRef:
    return UserRef(
        channel=Channel.TELEGRAM,
        user_id=user.id,
        username=user.username,
        display_name=user.full_name,
    )


def _inline_button(button: Button) -> InlineKeyboardButton:
    if button.kind is ButtonKind.URL:
        return InlineKeyboardButton(text=button.text, url=button.value)
    return InlineKeyboardButton(text=button.text, callback_data=button.value)


def build_markup(reply: Reply) -> TelegramMarkup:
    """Кнопка «поделиться номером» в Telegram бывает только в нижней (reply) клавиатуре."""
    if any(b.kind is ButtonKind.REQUEST_CONTACT for b in reply.buttons):
        rows = [
            [KeyboardButton(text=b.text, request_contact=True) for b in row if b.kind is ButtonKind.REQUEST_CONTACT]
            for row in reply.keyboard
        ]
        return ReplyKeyboardMarkup(keyboard=[row for row in rows if row], resize_keyboard=True, one_time_keyboard=True)
    if reply.keyboard:
        return InlineKeyboardMarkup(inline_keyboard=[[_inline_button(b) for b in row] for row in reply.keyboard])
    if reply.clear_keyboard:
        return ReplyKeyboardRemove()
    return None


class TelegramNotifier:
    name = "telegram"

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def notify_admin(self, text: str) -> None:
        await self._bot.send_message(self._chat_id, text)


class _AdminFilter(Filter):
    def __init__(self, access: AdminAccess) -> None:
        self._access = access

    async def __call__(self, message: Message) -> bool:
        user_id = message.from_user.id if message.from_user else None
        return self._access.is_admin(message.chat.id, user_id)


class TelegramTransport:
    ALLOWED_UPDATES = ["message", "callback_query"]

    def __init__(
        self,
        token: str,
        service: LeadService,
        admin: AdminAccess,
        *,
        session: BaseSession | None = None,
    ) -> None:
        self._service = service
        self._admin = admin
        self.bot = Bot(
            token=token,
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(self._build_router())

    def notifier(self) -> TelegramNotifier | None:
        return TelegramNotifier(self.bot, self._admin.chat_id) if self._admin.chat_id is not None else None

    async def setup_commands(self) -> None:
        """Подсказки команд в меню. Ошибка здесь не мешает работе бота."""
        try:
            await self.bot.set_my_commands(list(PRIVATE_COMMANDS), scope=BotCommandScopeAllPrivateChats())
            if self._admin.chat_id is not None:
                await self.bot.set_my_commands(
                    list(ADMIN_COMMANDS), scope=BotCommandScopeChat(chat_id=self._admin.chat_id)
                )
        except TelegramAPIError as exc:
            log.warning("Telegram: не удалось задать меню команд: %s", exc)

    async def run(self) -> None:
        me = await self.bot.get_me()  # заодно проверка токена: при ошибке — TelegramUnauthorizedError
        log.info("Telegram: бот @%s запущен (long polling)", me.username)
        await self.setup_commands()
        await self.dispatcher.start_polling(
            self.bot, handle_signals=False, close_bot_session=False, allowed_updates=self.ALLOWED_UPDATES
        )

    async def close(self) -> None:
        await self.bot.session.close()

    # --- обработчики ---------------------------------------------------------------------------

    def _build_router(self) -> Router:
        router = Router(name="leads-telegram")
        private = F.chat.type == ChatType.PRIVATE
        is_admin = _AdminFilter(self._admin)

        router.message.register(self._on_start, CommandStart(), private)
        router.message.register(self._on_cancel, Command("cancel"), private)
        router.message.register(self._on_forget, Command("forget"), private)
        router.message.register(self._on_id, Command("id"))
        router.message.register(self._on_stats, Command("stats"), is_admin)
        router.message.register(self._on_export, Command("export"), is_admin)
        router.message.register(self._on_contact, F.contact, private)
        router.message.register(self._on_text, F.text, private)
        router.message.register(self._on_other, private)
        router.callback_query.register(self._on_callback, F.data)
        return router

    async def _send(self, chat_id: int, replies: Sequence[Reply]) -> None:
        for reply in replies:
            await self.bot.send_message(chat_id, reply.text, reply_markup=build_markup(reply))

    async def _on_start(self, message: Message) -> None:
        if message.from_user:
            await self._send(message.chat.id, await self._service.start(user_ref(message.from_user)))

    async def _on_cancel(self, message: Message) -> None:
        if message.from_user:
            await self._send(message.chat.id, await self._service.cancel(user_ref(message.from_user)))

    async def _on_forget(self, message: Message) -> None:
        if message.from_user:
            await self._send(message.chat.id, await self._service.forget(user_ref(message.from_user)))

    async def _on_contact(self, message: Message) -> None:
        if message.from_user and message.contact:
            replies = await self._service.handle_contact(user_ref(message.from_user), message.contact.phone_number)
            await self._send(message.chat.id, replies)

    async def _on_text(self, message: Message) -> None:
        if message.from_user and message.text is not None:
            replies = await self._service.handle_text(user_ref(message.from_user), message.text)
            await self._send(message.chat.id, replies)

    async def _on_other(self, message: Message) -> None:
        if message.from_user:
            await self._send(message.chat.id, await self._service.handle_other(user_ref(message.from_user)))

    async def _on_callback(self, callback: CallbackQuery) -> None:
        try:
            await callback.answer()
        except TelegramAPIError as exc:  # например, кнопку нажали слишком давно
            log.debug("Telegram: answerCallbackQuery не прошёл: %s", exc)

        message = callback.message
        if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
            return
        try:
            # Убираем кнопки под нажатым сообщением, чтобы их не нажимали повторно.
            await message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest as exc:
            log.debug("Telegram: не удалось убрать кнопки: %s", exc)

        replies = await self._service.handle_button(user_ref(callback.from_user), callback.data or "")
        await self._send(message.chat.id, replies)

    async def _on_id(self, message: Message) -> None:
        user_id = message.from_user.id if message.from_user else "—"
        await message.answer(CHAT_ID_REPLY.format(chat_id=message.chat.id, user_id=user_id))

    async def _on_stats(self, message: Message) -> None:
        await message.answer(self._service.stats_text())

    async def _on_export(self, message: Message) -> None:
        export = self._service.export()
        if export.count == 0:
            await message.answer(ADMIN_NO_LEADS)
            return
        await message.answer_document(
            BufferedInputFile(export.content, filename=export.filename),
            caption=ADMIN_EXPORT_CAPTION.format(count=export.count),
        )
        log.info("Telegram: выгрузка %d заявок отправлена в чат %s", export.count, message.chat.id)
