"""Human-readable labels; persisted domain values remain unchanged."""

LABELS = {
    "observe": "Наблюдение",
    "auto": "Боевой",
    "paused": "Пауза",
    "connected": "Подключена",
    "archived": "В архиве",
    "pending": "Ожидает обработки",
    "open": "Ожидает владельца",
    "closed": "Закрыт владельцем",
    "disconnected": "Отключена · история сохранена",
    "submitted": "На рассмотрении владельца",
    "rejected": "Отклонена",
    "withdrawn": "Отозвана",
    "awaiting_delivery": "Ожидает доставки карточки в админку",
    "healthy": "Всё в порядке",
    "degraded": "Требует внимания",
    "owner": "Владелец системы",
    "manager": "Управляющий",
    "moderator": "Модератор",
    "observer": "Наблюдатель",
    "checked": "Проверено",
    "review": "Нужно решение модератора",
    "error": "Не удалось проверить",
    "skipped": "Проверка пропущена",
    "legacy": "Историческая запись",
    "SCAM": "Скам",
    "NOT_SCAM": "Не скам",
    "OK": "Признаков скама не найдено",
    "REVIEW": "Нужно рассмотрение",
    "administrator": "Автор — администратор группы",
    "trusted": "Участник в вайтлисте этой группы — проверка не требуется",
    "membership_unavailable": "Не удалось получить статус участника в Telegram. Автоматическая проверка не выполнена",
    "whitelisted": "Участнику выдано доверие",
    "provider_not_configured": "Ключ модели ещё не настроен",
    "planned": "Запланировано",
    "in_flight": "Выполняется",
    "succeeded": "Выполнено",
    "failed": "Не выполнено",
    "unknown": "Результат неизвестен — нужна сверка",
    "cancelled": "Отменено",
    "delete": "Удаление",
    "warn": "Уведомление в группе",
    "ban": "Бан",
    "unban": "Разбан",
    "mute": "Мут",
    "unmute": "Размут",
}


def label(value):
    return LABELS.get(str(value), str(value)) if value is not None else "—"


def telegram_member_status(member):
    """Restricted members can still write after restoring the group's permissions."""
    status = str(member.status)
    text = {
        "kicked": "забанен",
        "restricted": "ограничен",
        "left": "не состоит",
        "member": "участник",
        "administrator": "администратор",
        "creator": "владелец",
    }.get(status, status)
    muted = (
        status == "restricted" and getattr(member, "can_send_messages", None) is False
    )
    if status == "restricted":
        if getattr(member, "is_member", True) is False:
            return "не состоит", False
        if getattr(member, "can_send_messages", None) is True:
            return "писать разрешено · действуют индивидуальные права", False
        if muted:
            text = "писать запрещено"
        until = getattr(member, "until_date", None)
        if until is not None:
            text += f" до {until}" if until.timestamp() > 0 else " · бессрочно"
    return text, muted


def person(row):
    name = " ".join(filter(None, [row.get("first_name"), row.get("last_name")]))
    username = row.get("username")
    return (name + (f" · @{username}" if username else "")).strip() or (
        f"Участник {row.get('telegram_user_id') or row.get('user_id') or row.get('id')}"
    )


def setting_value(value):
    if isinstance(value, bool):
        return "Включено" if value else "Выключено"
    if value is None:
        return "Не задано"
    return str(value)


def button_text(text, data=""):
    """Decorate existing and new keyboards consistently without changing callbacks."""
    if not text or not (text[0].isalnum() or text[0] in {"@", "_"}):
        return text
    terms = (
        ("разбан", "🔓"),
        ("размут", "🔊"),
        ("снять мут", "🔊"),
        ("не скам", "✅"),
        ("скам", "🚫"),
        ("бан", "🔨"),
        ("мут", "🔇"),
        ("вайтлист", "🤍"),
        ("довер", "🤍"),
        ("блэклист", "⛔"),
        ("истор", "📜"),
        ("поиск", "🔍"),
        ("найти", "🔍"),
        ("участник", "👤"),
        ("команд", "🧑‍💼"),
        ("роли", "🧑‍💼"),
        ("настрой", "⚙️"),
        ("режим", "🛡"),
        ("пауза", "⏸"),
        ("наблюден", "👁"),
        ("боевой", "🛡"),
        ("удалить", "🗑"),
        ("бюджет", "💳"),
        ("расход", "💳"),
        ("подключ", "🔗"),
        ("заявк", "📨"),
        ("групп", "👥"),
        ("архив", "📦"),
        ("отмена", "❌"),
        ("назад", "⬅️"),
        ("меню", "🏠"),
        ("подтверд", "✅"),
        ("обнов", "🔄"),
        ("провер", "🔄"),
        ("подроб", "🔎"),
        ("ещё", "⋯"),
        ("состояние", "🩺"),
        ("уведом", "🔔"),
        ("сводк", "📊"),
        ("статист", "📊"),
    )
    lower = text.lower()
    icon = next((icon for term, icon in terms if term in lower), "▪️")
    return icon + " " + text
