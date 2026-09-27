"""Уведомление администратору о новой заявке."""

from __future__ import annotations

from html import escape
from typing import Protocol
from zoneinfo import ZoneInfo

from .models import Channel, Lead
from .phone import format_phone, mask_phone

# Ссылка на профиль пользователя в мессенджере — чтобы менеджер мог написать ему в один клик.
PROFILE_LINKS: dict[Channel, str] = {
    Channel.TELEGRAM: "tg://user?id={user_id}",
    Channel.MAX: "max://user/{user_id}",
}


class Notifier(Protocol):
    """Куда отправлять уведомления о заявках (админ-чат в Telegram, MAX и т.п.)."""

    name: str

    async def notify_admin(self, text: str) -> None: ...


def format_admin_notification(lead: Lead, tz: ZoneInfo, *, hide_phone: bool = False) -> str:
    """Текст уведомления в HTML (подмножество, общее для Telegram и MAX)."""
    phone = mask_phone(lead.phone) if hide_phone else format_phone(lead.phone)
    who = escape(lead.display_name or "профиль")
    if lead.username:
        who = f"{who} (@{escape(lead.username)})"
    link_template = PROFILE_LINKS.get(lead.channel)
    if link_template:
        profile_url = link_template.format(user_id=lead.user_id)
        who = f'<a href="{escape(profile_url, quote=True)}">{who}</a>'
    created = lead.created_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")

    lines = [
        f"<b>Новая заявка №{lead.id}</b>",
        "",
        f"<b>Услуга:</b> {escape(lead.service_title)}",
        f"<b>Имя:</b> {escape(lead.name)}",
        f"<b>Телефон:</b> {escape(phone)}",
    ]
    if lead.comment:
        lines.append(f"<b>Комментарий:</b> {escape(lead.comment)}")
    lines += [
        "",
        f"<b>Откуда:</b> {lead.channel.title}, {who}",
        f"<b>Время:</b> {created}",
    ]
    if hide_phone:
        lines.append("<i>Полный номер — в выгрузке /export.</i>")
    return "\n".join(lines)
