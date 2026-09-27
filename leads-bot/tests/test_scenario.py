"""Сценарий заявки шаг за шагом."""

from __future__ import annotations

from leads_bot.config import BotConfig
from leads_bot.core.models import ButtonKind, Channel, Step, UserRef
from leads_bot.core.service import LeadService, Payload
from leads_bot.core.storage import Storage
from tests.helpers import FakeClock, RecordingNotifier, fill_lead


async def test_full_scenario_step_by_step(
    service: LeadService, storage: Storage, user: UserRef, config: BotConfig, notifier: RecordingNotifier
) -> None:
    # 1. /start — сначала согласие, со ссылкой на политику; данных ещё нет.
    [consent] = await service.start(user)
    assert "согласие на обработку" in consent.text
    assert f'href="{config.privacy.policy_url}"' in consent.text
    url_button, yes, no = consent.buttons
    assert (url_button.kind, url_button.value) == (ButtonKind.URL, config.privacy.policy_url)
    assert (yes.value, no.value) == (Payload.CONSENT_YES, Payload.CONSENT_NO)
    dialog = storage.get_dialog(user.channel, user.user_id)
    assert dialog is not None and dialog.step is Step.CONSENT and dialog.data == {}

    # 2. Согласие → список услуг из конфига и кнопка отмены.
    [services] = await service.handle_button(user, Payload.CONSENT_YES)
    assert [b.text for b in services.buttons] == [s.title for s in config.services] + [config.texts.cancel_button]
    dialog = storage.get_dialog(user.channel, user.user_id)
    assert dialog is not None and dialog.data["policy_url"] == config.privacy.policy_url
    assert dialog.data["consent_at"] == "2026-09-27T09:00:00+00:00"

    # 3. Услуга → имя.
    [ask_name] = await service.handle_button(user, Payload.SERVICE_PREFIX + "bathroom")
    assert ask_name.text == config.texts.ask_name

    # 4. Имя (лишние пробелы убираются) → телефон с кнопкой «Отправить мой номер».
    [ask_phone] = await service.handle_text(user, "  Иван   Петров ")
    assert [b.kind for b in ask_phone.buttons] == [ButtonKind.REQUEST_CONTACT]

    # 5. Неверный номер → повторный запрос, шаг не меняется.
    [invalid] = await service.handle_text(user, "12345")
    assert invalid.text == config.texts.invalid_phone
    assert storage.get_dialog(user.channel, user.user_id).step is Step.PHONE  # type: ignore[union-attr]

    # 6. Верный номер → номер нормализован, клавиатура с номером убирается, просим комментарий.
    saved, ask_comment = await service.handle_text(user, "8 (999) 123-45-67")
    assert saved.clear_keyboard and "+7 999 123-45-67" in saved.text
    assert [b.value for b in ask_comment.buttons] == [Payload.SKIP_COMMENT]

    # 7. Комментарий → сводка; пользовательский ввод экранирован.
    [confirm] = await service.handle_text(user, "Плитка <b>уже</b> куплена")
    assert "Санузел и плитка" in confirm.text
    assert "Иван Петров" in confirm.text
    assert "Плитка &lt;b&gt;уже&lt;/b&gt; куплена" in confirm.text
    assert [b.value for b in confirm.buttons] == [Payload.SEND, Payload.EDIT, Payload.CANCEL]

    # 8. Отправить → заявка сохранена, диалог удалён, админ уведомлён.
    [accepted] = await service.handle_button(user, Payload.SEND)
    assert "№1" in accepted.text and "Иван Петров" in accepted.text
    assert storage.get_dialog(user.channel, user.user_id) is None
    [lead] = storage.list_leads()
    assert (lead.service_id, lead.name, lead.phone) == ("bathroom", "Иван Петров", "+79991234567")
    assert lead.comment == "Плитка <b>уже</b> куплена"  # в базе — как ввёл человек
    assert lead.channel is Channel.TELEGRAM and lead.username == "ivan"
    assert lead.policy_url == config.privacy.policy_url and lead.policy_version == config.privacy.policy_version
    assert lead.admin_notified
    assert len(notifier.messages) == 1 and "Новая заявка №1" in notifier.messages[0]


async def test_decline_consent_stores_nothing(
    service: LeadService, storage: Storage, user: UserRef, config: BotConfig
) -> None:
    await service.start(user)
    # Текст, присланный до согласия, не сохраняется.
    [reminder] = await service.handle_text(user, "Иван, +79991234567")
    assert reminder.text == config.texts.consent_required
    assert storage.get_dialog(user.channel, user.user_id).data == {}  # type: ignore[union-attr]

    [declined] = await service.handle_button(user, Payload.CONSENT_NO)
    assert config.company.phone in declined.text
    assert storage.get_dialog(user.channel, user.user_id) is None
    assert storage.count_leads() == 0

    # Кнопки после отказа не работают, текст — подсказка начать заново.
    [stale] = await service.handle_button(user, Payload.SERVICE_PREFIX + "other")
    assert stale.text == config.texts.stale_button
    [idle] = await service.handle_text(user, "Иван")
    assert [b.value for b in idle.buttons] == [Payload.START]


async def test_skip_comment(service: LeadService, storage: Storage, user: UserRef, config: BotConfig) -> None:
    [confirm] = await fill_lead(service, user, comment=None, send=False)
    assert f"<b>Комментарий:</b> {config.texts.no_comment}" in confirm.text
    await service.handle_button(user, Payload.SEND)
    assert storage.list_leads()[0].comment == ""


async def test_contact_button_phone(service: LeadService, storage: Storage, user: UserRef) -> None:
    await service.start(user)
    await service.handle_button(user, Payload.CONSENT_YES)
    await service.handle_button(user, Payload.SERVICE_PREFIX + "design")
    await service.handle_text(user, "Анна")
    saved, _ = await service.handle_contact(user, "79161234567")  # Telegram присылает номер без «+»
    assert saved.clear_keyboard
    assert storage.get_dialog(user.channel, user.user_id).data["phone"] == "+79161234567"  # type: ignore[union-attr]


async def test_foreign_contact_rejected(service: LeadService, storage: Storage, user: UserRef, config: BotConfig) -> None:
    await service.start(user)
    await service.handle_button(user, Payload.CONSENT_YES)
    await service.handle_button(user, Payload.SERVICE_PREFIX + "design")
    await service.handle_text(user, "Анна")
    [reply] = await service.handle_contact(user, "+380501234567")
    assert reply.text == config.texts.invalid_phone


async def test_invalid_name_and_long_comment(service: LeadService, user: UserRef, config: BotConfig) -> None:
    await service.start(user)
    await service.handle_button(user, Payload.CONSENT_YES)
    await service.handle_button(user, Payload.SERVICE_PREFIX + "other")
    [bad_name] = await service.handle_text(user, "1234")
    assert bad_name.text == config.texts.invalid_name.format(min_length=2, max_length=60)
    await service.handle_text(user, "Анна-Мария")
    await service.handle_text(user, "+79991234567")
    [too_long] = await service.handle_text(user, "x" * 1001)
    assert too_long.text == config.texts.comment_too_long.format(max_length=1000)


async def test_service_can_be_typed(service: LeadService, user: UserRef, config: BotConfig) -> None:
    await service.start(user)
    await service.handle_button(user, Payload.CONSENT_YES)
    [ask_name] = await service.handle_text(user, "  электрика ")
    assert ask_name.text == config.texts.ask_name
    # Незнакомый текст — просим выбрать кнопкой и показываем список снова.
    await service.start(user)
    await service.handle_button(user, Payload.CONSENT_YES)
    hint, again = await service.handle_text(user, "покраска забора")
    assert hint.text == config.texts.use_buttons and again.buttons


async def test_edit_restarts_but_keeps_consent(service: LeadService, storage: Storage, user: UserRef) -> None:
    await fill_lead(service, user, send=False)
    [services] = await service.handle_button(user, Payload.EDIT)
    assert services.buttons[0].value.startswith(Payload.SERVICE_PREFIX)
    dialog = storage.get_dialog(user.channel, user.user_id)
    assert dialog is not None and dialog.step is Step.SERVICE
    assert set(dialog.data) == {"consent_at", "policy_url", "policy_version"}


async def test_cancel_at_any_step(service: LeadService, storage: Storage, user: UserRef, config: BotConfig) -> None:
    await service.start(user)
    await service.handle_button(user, Payload.CONSENT_YES)
    await service.handle_button(user, Payload.SERVICE_PREFIX + "other")
    await service.handle_text(user, "Иван")
    [cancelled] = await service.cancel(user)
    assert cancelled.text == config.texts.cancelled and cancelled.clear_keyboard
    assert storage.get_dialog(user.channel, user.user_id) is None
    # Кнопка «Отменить» делает то же самое.
    await fill_lead(service, user, send=False)
    await service.handle_button(user, Payload.CANCEL)
    assert storage.get_dialog(user.channel, user.user_id) is None
    assert storage.count_leads() == 0


async def test_double_send_creates_one_lead(service: LeadService, storage: Storage, user: UserRef, config: BotConfig) -> None:
    await fill_lead(service, user)
    [second] = await service.handle_button(user, Payload.SEND)
    assert second.text == config.texts.stale_button
    assert storage.count_leads() == 1


async def test_buttons_from_wrong_step_are_stale(service: LeadService, user: UserRef, config: BotConfig) -> None:
    await service.start(user)
    [reply] = await service.handle_button(user, Payload.SEND)
    assert reply.text == config.texts.stale_button
    await service.handle_button(user, Payload.CONSENT_YES)
    [reply] = await service.handle_button(user, Payload.CONSENT_YES)  # повторное нажатие
    assert reply.text == config.texts.stale_button
    [reply] = await service.handle_button(user, Payload.SERVICE_PREFIX + "no-such-service")
    assert reply.text == config.texts.stale_button


async def test_unexpected_input_repeats_current_step(service: LeadService, user: UserRef, config: BotConfig) -> None:
    await service.start(user)
    await service.handle_button(user, Payload.CONSENT_YES)
    await service.handle_button(user, Payload.SERVICE_PREFIX + "other")
    [reply] = await service.handle_other(user)  # стикер вместо имени
    assert reply.text == config.texts.ask_name
    [reply] = await service.handle_contact(user, "+79991234567")  # контакт вместо имени
    assert reply.text == config.texts.ask_name


async def test_abandoned_dialog_expires(
    service: LeadService, storage: Storage, user: UserRef, clock: FakeClock, config: BotConfig
) -> None:
    await service.start(user)
    await service.handle_button(user, Payload.CONSENT_YES)
    clock.advance(hours=config.dialog_ttl_hours + 1)
    [reply] = await service.handle_button(user, Payload.SERVICE_PREFIX + "other")
    assert reply.text == config.texts.stale_button
    assert storage.get_dialog(user.channel, user.user_id) is None


async def test_forget_deletes_user_data(service: LeadService, storage: Storage, user: UserRef, clock: FakeClock) -> None:
    other = UserRef(channel=Channel.TELEGRAM, user_id=2002)
    await fill_lead(service, user)
    clock.advance(minutes=1)
    await fill_lead(service, other)
    await service.start(user)
    await service.handle_button(user, Payload.CONSENT_YES)

    [reply] = await service.forget(user)
    assert "заявок: 1" in reply.text
    assert storage.get_dialog(user.channel, user.user_id) is None
    assert [lead.user_id for lead in storage.list_leads()] == [2002]


async def test_channels_are_separate(service: LeadService, storage: Storage) -> None:
    telegram_user = UserRef(channel=Channel.TELEGRAM, user_id=5)
    max_user = UserRef(channel=Channel.MAX, user_id=5)  # тот же id в другом мессенджере — другой человек
    await service.start(telegram_user)
    await service.start(max_user)
    await service.handle_button(max_user, Payload.CONSENT_YES)
    assert storage.get_dialog(Channel.TELEGRAM, 5).step is Step.CONSENT  # type: ignore[union-attr]
    assert storage.get_dialog(Channel.MAX, 5).step is Step.SERVICE  # type: ignore[union-attr]
