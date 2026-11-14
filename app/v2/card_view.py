"""Plain-text Telegram cards. Only the real Telegram ID receives a code entity."""

from datetime import timezone
from zoneinfo import ZoneInfo

from aiogram.types import MessageEntity

from app.v2.presentation import label, person
from app.v2.domain import Policy


def card_datetime(value, timezone_name):
    # Legacy naive timestamps are UTC, never the timezone of the server process.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(ZoneInfo(timezone_name or Policy().timezone))
    months = (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    )
    return f"{local.day} {months[local.month - 1]} {local.year}, {local:%H:%M}"


def code_entities(text, telegram_id):
    if telegram_id is None:
        return []
    marker = f"({telegram_id})"
    # The ID belongs to the author line, never a number inside untrusted content.
    line = next((line for line in text.splitlines() if line.startswith("👤 ")), "")
    if not line.endswith(marker):
        return []
    start = text.index(line) + len(line) - len(marker) + 1
    return [
        MessageEntity(
            type="code",
            offset=len(text[:start].encode("utf-16-le")) // 2,
            length=len(str(telegram_id)),
        )
    ]


def author(row):
    if not row.get("user_id"):
        return "👤 Канал или анонимный автор"
    # Keep the header bounded independently of the publication body.
    name = person(row).replace("\n", " ").replace("\r", " ")[:160]
    tid = row.get("telegram_user_id")
    return f"👤 {name}" + (f" ({tid})" if tid is not None else "")


def incident_text(row, title=None):
    if row.get("human_label"):
        heading = (
            "✅ Не скам" if row["human_label"] == "NOT_SCAM" else "🚫 Скам подтверждён"
        )
    elif row.get("status") == "error":
        heading = "⚠️ Не удалось проверить сообщение"
    else:
        heading = "⚠️ Обнаружен вероятный скам"
    lines = [
        heading,
        "",
        "👥 " + str(title or row.get("group_title") or row["chat_id"])[:180],
        author(row),
    ]
    if row.get("created_at"):
        lines.append("🕒 " + card_datetime(row["created_at"], row.get("timezone")))
    lines.append("")
    for item in row.get("texts", [])[:2]:
        if item.get("text"):
            lines.append(
                item["text"]
                .encode("utf-16-le")[:1000]
                .decode("utf-16-le", errors="ignore")
            )
        if item.get("ocr_text"):
            lines.append(
                "🖼 Текст изображения: "
                + item["ocr_text"]
                .encode("utf-16-le")[:500]
                .decode("utf-16-le", errors="ignore")
            )
    confidence = row.get("confidence")
    lines += [
        "",
        "📝 Причина: " + label(row.get("reason"))[:400],
        "📊 Уверенность модели: "
        + (f"{confidence:.0%}" if confidence is not None else "не указана"),
    ]
    if row.get("human_label"):
        lines.append("🧑‍💼 Решение модератора: " + label(row["human_label"]))
    if row.get("trust_grant"):
        lines.append("🤍 Участник в вайтлисте этой группы.")
    lines.append(
        "⚠️ Страйк за этот случай: "
        + ("учтён" if row.get("active_strike") else "не начислен")
    )
    actions = row.get("actions", [])
    if actions:
        lines.extend(
            "• " + label(a["kind"]) + ": " + label(a["state"]) for a in actions[-6:]
        )
    else:
        lines.append("ℹ️ Наказания по этому случаю не запланированы.")
    if row.get("legacy"):
        lines.append(
            "📦 История до обновления. Новые наказания автоматически не применяются."
        )
    for link in row.get("message_links", [])[:1]:
        lines.append("🔗 Оригинал: " + link)
    return "\n".join(lines)


def trust_text(row, title, credits, threshold):
    heading = (
        "🤍 Добавлен в вайтлист автоматически"
        if not row.get("revoked_at")
        else "↩️ Автоматическое доверие снято"
    )
    return "\n".join(
        [
            heading,
            "",
            "👥 " + title,
            author(row),
            "",
            f"✅ Проверенных безопасных сообщений: {credits} из {threshold}.",
            "📅 Доверие: бессрочно."
            if not row.get("revoked_at")
            else "🔍 Проверки возобновлены. История сохранена.",
            "ℹ️ Действует только в этой рабочей группе.",
        ]
    )
