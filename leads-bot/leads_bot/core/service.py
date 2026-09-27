"""Сценарий заявки — сердце бота, не зависит от мессенджера.

Шаги: согласие на обработку ПДн → услуга → имя → телефон → комментарий →
подтверждение. До согласия бот ничего не сохраняет: текст, присланный на шаге
согласия, отбрасывается.

Транспорт вызывает методы `start`, `cancel`, `forget`, `handle_text`,
`handle_contact`, `handle_button` и отрисовывает полученный список `Reply`.
Все переходы состояния делаются синхронно, без `await` между чтением и записью
диалога, — поэтому двойное нажатие «Отправить» не создаст две заявки.
Единственный `await` — рассылка уведомления администратору, уже после
сохранения заявки.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from html import escape

from ..config import BotConfig, Service
from .export import export_filename, export_leads_xlsx
from .models import Button, Dialog, ExportFile, Lead, LeadDraft, Reply, Step, UserRef
from .notify import Notifier, format_admin_notification
from .phone import PhoneValidationError, format_phone, normalize_phone
from .stats import collect_stats, format_stats
from .storage import Storage, from_db_time, to_db_time
from .validation import (
    COMMENT_MAX_LENGTH,
    NAME_MAX_LENGTH,
    NAME_MIN_LENGTH,
    CommentTooLongError,
    NameValidationError,
    normalize_comment,
    normalize_name,
)

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
RATE_LIMIT_WINDOW = timedelta(hours=1)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Payload:
    """Данные callback-кнопок. Короткие: у Telegram лимит 64 байта."""

    START = "start"
    CANCEL = "cancel"
    CONSENT_YES = "consent:yes"
    CONSENT_NO = "consent:no"
    SERVICE_PREFIX = "service:"
    SKIP_COMMENT = "comment:skip"
    SEND = "confirm:send"
    EDIT = "confirm:edit"


_REQUIRED_FOR_SUBMIT = ("consent_at", "policy_url", "service_id", "service_title", "name", "phone")


class LeadService:
    def __init__(
        self,
        config: BotConfig,
        storage: Storage,
        *,
        notifiers: Sequence[Notifier] = (),
        clock: Clock = utc_now,
    ) -> None:
        self._config = config
        self._texts = config.texts
        self._storage = storage
        self._notifiers: list[Notifier] = list(notifiers)
        self._clock = clock

    @property
    def config(self) -> BotConfig:
        return self._config

    @property
    def storage(self) -> Storage:
        return self._storage

    def add_notifier(self, notifier: Notifier) -> None:
        self._notifiers.append(notifier)

    # --- события пользователя ---------------------------------------------------------------

    async def start(self, user: UserRef) -> list[Reply]:
        self._storage.delete_dialog(user.channel, user.user_id)
        limited = self._rate_limit_reply(user)
        if limited is not None:
            return [limited]
        self._save(Dialog(channel=user.channel, user_id=user.user_id, step=Step.CONSENT, updated_at=self._clock()))
        return [self._consent_reply()]

    async def cancel(self, user: UserRef) -> list[Reply]:
        self._storage.delete_dialog(user.channel, user.user_id)
        return [Reply(self._texts.cancelled, clear_keyboard=True)]

    async def forget(self, user: UserRef) -> list[Reply]:
        """Отзыв согласия: удалить все заявки и незаконченный диалог пользователя."""
        deleted = self._storage.delete_user_data(user.channel, user.user_id)
        log.info("Данные пользователя удалены по его запросу: канал=%s, заявок=%d", user.channel.value, deleted)
        return [Reply(self._texts.data_deleted.format(count=deleted), clear_keyboard=True)]

    async def handle_text(self, user: UserRef, text: str) -> list[Reply]:
        dialog = self._active_dialog(user)
        if dialog is None:
            return [self._idle_reply()]

        if dialog.step is Step.CONSENT:
            # До согласия ничего не сохраняем — даже то, что человек успел написать.
            return [Reply(self._texts.consent_required)]

        if dialog.step is Step.SERVICE:
            service = self._service_by_title(text)
            if service is not None:
                return self._choose_service(dialog, service)
            return [Reply(self._texts.use_buttons), self._service_reply()]

        if dialog.step is Step.NAME:
            try:
                dialog.data["name"] = normalize_name(text)
            except NameValidationError:
                return [
                    Reply(self._texts.invalid_name.format(min_length=NAME_MIN_LENGTH, max_length=NAME_MAX_LENGTH))
                ]
            dialog.step = Step.PHONE
            self._save(dialog)
            return [self._phone_reply()]

        if dialog.step is Step.PHONE:
            return self._accept_phone(dialog, text)

        if dialog.step is Step.COMMENT:
            try:
                comment = normalize_comment(text)
            except CommentTooLongError:
                return [Reply(self._texts.comment_too_long.format(max_length=COMMENT_MAX_LENGTH))]
            return self._to_confirm(dialog, comment)

        return [Reply(self._texts.use_buttons)]  # Step.CONFIRM

    async def handle_contact(self, user: UserRef, phone: str) -> list[Reply]:
        """Пользователь нажал «Отправить мой номер» (контакт из мессенджера)."""
        dialog = self._active_dialog(user)
        if dialog is None:
            return [self._idle_reply()]
        if dialog.step is Step.PHONE:
            return self._accept_phone(dialog, phone)
        return self._repeat_step(dialog)

    async def handle_other(self, user: UserRef) -> list[Reply]:
        """Стикер, фото, голосовое — повторить подсказку текущего шага."""
        dialog = self._active_dialog(user)
        if dialog is None:
            return [self._idle_reply()]
        return self._repeat_step(dialog)

    async def handle_button(self, user: UserRef, payload: str) -> list[Reply]:
        if payload == Payload.START:
            return await self.start(user)
        if payload == Payload.CANCEL:
            return await self.cancel(user)

        dialog = self._active_dialog(user)
        if dialog is None:
            return [Reply(self._texts.stale_button)]

        if dialog.step is Step.CONSENT and payload == Payload.CONSENT_YES:
            now = self._clock()
            dialog.data.update(
                consent_at=to_db_time(now),
                policy_url=self._config.privacy.policy_url,
                policy_version=self._config.privacy.policy_version,
            )
            dialog.step = Step.SERVICE
            self._save(dialog)
            return [self._service_reply()]

        if dialog.step is Step.CONSENT and payload == Payload.CONSENT_NO:
            self._storage.delete_dialog(user.channel, user.user_id)
            return [
                Reply(
                    self._texts.consent_declined.format(
                        company=escape(self._config.company.name),
                        company_phone=escape(self._config.company.phone),
                    ),
                    clear_keyboard=True,
                )
            ]

        if dialog.step is Step.SERVICE and payload.startswith(Payload.SERVICE_PREFIX):
            service = self._config.service_by_id(payload.removeprefix(Payload.SERVICE_PREFIX))
            if service is not None:
                return self._choose_service(dialog, service)

        if dialog.step is Step.COMMENT and payload == Payload.SKIP_COMMENT:
            return self._to_confirm(dialog, "")

        if dialog.step is Step.CONFIRM and payload == Payload.SEND:
            return await self._submit(user, dialog)

        if dialog.step is Step.CONFIRM and payload == Payload.EDIT:
            consent = {k: v for k, v in dialog.data.items() if k in ("consent_at", "policy_url", "policy_version")}
            dialog.data = consent
            dialog.step = Step.SERVICE
            self._save(dialog)
            return [self._service_reply()]

        return [Reply(self._texts.stale_button)]

    # --- администратор ----------------------------------------------------------------------

    def stats_text(self) -> str:
        tz = self._config.tz
        return format_stats(collect_stats(self._storage, self._clock(), tz), tz)

    def export(self) -> ExportFile:
        tz = self._config.tz
        leads = self._storage.list_leads()
        return ExportFile(
            filename=export_filename(self._clock(), tz),
            content=export_leads_xlsx(leads, tz),
            count=len(leads),
        )

    def run_maintenance(self) -> None:
        """Удалить брошенные диалоги и (если задан срок хранения) старые заявки."""
        now = self._clock()
        dialogs = self._storage.purge_dialogs(now - timedelta(hours=self._config.dialog_ttl_hours))
        leads = 0
        if self._config.retention_days:
            leads = self._storage.purge_leads(now - timedelta(days=self._config.retention_days))
        if dialogs or leads:
            log.info("Очистка: удалено брошенных диалогов — %d, заявок старше срока хранения — %d", dialogs, leads)

    # --- внутреннее --------------------------------------------------------------------------

    def _active_dialog(self, user: UserRef) -> Dialog | None:
        dialog = self._storage.get_dialog(user.channel, user.user_id)
        if dialog is None:
            return None
        if self._clock() - dialog.updated_at > timedelta(hours=self._config.dialog_ttl_hours):
            self._storage.delete_dialog(user.channel, user.user_id)
            return None
        return dialog

    def _save(self, dialog: Dialog) -> None:
        dialog.updated_at = self._clock()
        self._storage.save_dialog(dialog)

    def _service_by_title(self, text: str) -> Service | None:
        wanted = text.strip().casefold()
        return next((s for s in self._config.services if s.title.casefold() == wanted), None)

    def _choose_service(self, dialog: Dialog, service: Service) -> list[Reply]:
        dialog.data.update(service_id=service.id, service_title=service.title)
        dialog.step = Step.NAME
        self._save(dialog)
        return [Reply(self._texts.ask_name)]

    def _accept_phone(self, dialog: Dialog, raw: str) -> list[Reply]:
        try:
            phone = normalize_phone(raw)
        except PhoneValidationError:
            return [Reply(self._texts.invalid_phone)]
        dialog.data["phone"] = phone
        dialog.step = Step.COMMENT
        self._save(dialog)
        return [
            Reply(self._texts.phone_saved.format(phone=escape(format_phone(phone))), clear_keyboard=True),
            self._comment_reply(),
        ]

    def _to_confirm(self, dialog: Dialog, comment: str) -> list[Reply]:
        dialog.data["comment"] = comment
        dialog.step = Step.CONFIRM
        self._save(dialog)
        return [self._confirm_reply(dialog)]

    async def _submit(self, user: UserRef, dialog: Dialog) -> list[Reply]:
        data = dialog.data
        if any(not data.get(key) for key in _REQUIRED_FOR_SUBMIT):
            self._storage.delete_dialog(user.channel, user.user_id)
            log.warning("Неполный диалог при отправке (канал %s) — сброшен", user.channel.value)
            return [Reply(self._texts.stale_button)]

        limited = self._rate_limit_reply(user)
        if limited is not None:
            self._storage.delete_dialog(user.channel, user.user_id)
            return [limited]

        lead = self._storage.add_lead(
            LeadDraft(
                created_at=self._clock(),
                channel=user.channel,
                user_id=user.user_id,
                username=user.username,
                display_name=user.display_name,
                service_id=data["service_id"],
                service_title=data["service_title"],
                name=data["name"],
                phone=data["phone"],
                comment=data.get("comment", ""),
                consent_at=from_db_time(data["consent_at"]),
                policy_url=data["policy_url"],
                policy_version=data.get("policy_version", ""),
            )
        )
        self._storage.delete_dialog(user.channel, user.user_id)
        log.info("Заявка №%d принята: канал=%s, услуга=%s", lead.id, lead.channel.value, lead.service_id)

        await self._notify(lead)
        return [Reply(self._texts.lead_accepted.format(name=escape(lead.name), lead_id=lead.id))]

    async def _notify(self, lead: Lead) -> None:
        if not self._notifiers:
            log.warning("Заявка №%d сохранена, но админ-чат не настроен — уведомление не отправлено", lead.id)
            return
        text = format_admin_notification(lead, self._config.tz, hide_phone=self._config.notifications.mask_phone)
        delivered = False
        for notifier in self._notifiers:
            try:
                await notifier.notify_admin(text)
                delivered = True
            except Exception:  # уведомление не должно ронять приём заявки — она уже сохранена
                log.exception("Не удалось отправить уведомление о заявке №%d через %s", lead.id, notifier.name)
        if delivered:
            self._storage.mark_notified(lead.id)

    def _rate_limit_reply(self, user: UserRef) -> Reply | None:
        now = self._clock()
        limit = self._config.antispam.max_leads_per_hour
        times = self._storage.user_lead_times_after(user.channel, user.user_id, now - RATE_LIMIT_WINDOW)
        if len(times) < limit:
            return None
        # Счётчик опустится ниже лимита, когда из окна выйдет заявка times[-limit].
        wait = times[-limit] + RATE_LIMIT_WINDOW - now
        minutes = max(1, math.ceil(wait.total_seconds() / 60))
        log.info("Антиспам: отклонена новая заявка (канал %s, заявок за час: %d)", user.channel.value, len(times))
        return Reply(
            self._texts.rate_limited.format(
                count=len(times), minutes=minutes, company_phone=escape(self._config.company.phone)
            ),
            clear_keyboard=True,
        )

    def _repeat_step(self, dialog: Dialog) -> list[Reply]:
        if dialog.step is Step.CONSENT:
            return [Reply(self._texts.consent_required)]
        if dialog.step is Step.SERVICE:
            return [self._service_reply()]
        if dialog.step is Step.NAME:
            return [Reply(self._texts.ask_name)]
        if dialog.step is Step.PHONE:
            return [self._phone_reply()]
        if dialog.step is Step.COMMENT:
            return [self._comment_reply()]
        return [Reply(self._texts.use_buttons)]

    # --- ответы ------------------------------------------------------------------------------

    def _consent_reply(self) -> Reply:
        t = self._texts
        policy_url = self._config.privacy.policy_url
        return Reply(
            t.consent.format(company=escape(self._config.company.name), policy_url=escape(policy_url, quote=True)),
            keyboard=(
                (Button.url(t.policy_button, policy_url),),
                (
                    Button.callback(t.consent_accept_button, Payload.CONSENT_YES),
                    Button.callback(t.consent_decline_button, Payload.CONSENT_NO),
                ),
            ),
        )

    def _service_reply(self) -> Reply:
        rows = tuple((Button.callback(s.title, Payload.SERVICE_PREFIX + s.id),) for s in self._config.services)
        return Reply(
            self._texts.choose_service,
            keyboard=rows + ((Button.callback(self._texts.cancel_button, Payload.CANCEL),),),
        )

    def _phone_reply(self) -> Reply:
        return Reply(self._texts.ask_phone, keyboard=((Button.request_contact(self._texts.share_contact_button),),))

    def _comment_reply(self) -> Reply:
        return Reply(
            self._texts.ask_comment, keyboard=((Button.callback(self._texts.skip_button, Payload.SKIP_COMMENT),),)
        )

    def _confirm_reply(self, dialog: Dialog) -> Reply:
        t = self._texts
        data = dialog.data
        comment = data.get("comment") or t.no_comment
        return Reply(
            t.confirm.format(
                service=escape(data.get("service_title", "")),
                name=escape(data.get("name", "")),
                phone=escape(format_phone(data.get("phone", ""))),
                comment=escape(comment),
            ),
            keyboard=(
                (Button.callback(t.send_button, Payload.SEND),),
                (Button.callback(t.edit_button, Payload.EDIT), Button.callback(t.cancel_button, Payload.CANCEL)),
            ),
        )

    def _idle_reply(self) -> Reply:
        return Reply(
            self._texts.idle_hint.format(company=escape(self._config.company.name)),
            keyboard=((Button.callback(self._texts.start_button, Payload.START),),),
        )
