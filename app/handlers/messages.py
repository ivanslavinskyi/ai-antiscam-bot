import logging
import datetime as dt
import html

from aiogram import Router, Bot
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatPermissions,
)
from sqlalchemy import select

from app.moderation.llm_client import classify_text
from app.db.session import get_session
from app.db import models as db
from app.config import settings
from app.moderation.schemas import LlmModerationResult

router = Router(name="messages-router")
logger = logging.getLogger(__name__)

# Пороговые значения для эскалации
MAX_STRIKES_WARN = 1      # 1-й скам — предупреждение
MAX_STRIKES_MUTE = 2      # 2-й скам — временный мут
MAX_STRIKES_BAN = 3       # 3-й и далее — бан
MUTE_HOURS = 24           # длительность мута (часов)


def parse_global_admin_chat_ids() -> list[int]:
    """
    Глобальные админ-чаты (суперадмины) из .env: admin_chat_ids.
    Они видят все карточки по всем чатам.
    """
    raw = settings.admin_chat_ids
    if not raw:
        return []
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            logger.warning("Invalid admin_chat_ids entry: %r", part)
    return ids


def is_admin_chat_id(chat_id: int) -> bool:
    """
    Является ли чат админ-чатом:
    - либо глобальным (из .env),
    - либо привязан к какому-то рабочему чату как admin_chat_telegram_id.
    """
    if chat_id in parse_global_admin_chat_ids():
        return True

    try:
        with get_session() as session:
            exists = session.execute(
                select(db.Chat.id).where(db.Chat.admin_chat_telegram_id == chat_id)
            ).first()
            return exists is not None
    except Exception as exc:
        logger.error(
            "Failed to check is_admin_chat_id for chat_id=%s: %s",
            chat_id,
            exc,
            exc_info=True,
        )
        return False


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Проверяем, является ли пользователь админом в чате."""
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as exc:
        logger.warning(
            "Failed to get chat administrators for chat_id=%s: %s",
            chat_id,
            exc,
        )
        return False

    admin_ids = {m.user.id for m in admins}
    return user_id in admin_ids


async def is_whitelisted(chat_id: int, user_id: int) -> bool:
    """
    Проверяем, находится ли пользователь в whitelist:
    - глобальный (User.is_global_whitelisted)
    - или в конкретном чате (ChatMember.is_whitelisted).
    """
    try:
        with get_session() as session:
            user = session.execute(
                select(db.User).where(db.User.telegram_user_id == user_id)
            ).scalar_one_or_none()
            if user is None:
                return False

            if user.is_global_whitelisted:
                return True

            chat = session.execute(
                select(db.Chat).where(db.Chat.telegram_chat_id == chat_id)
            ).scalar_one_or_none()
            if chat is None:
                return False

            member = session.execute(
                select(db.ChatMember).where(
                    db.ChatMember.chat_id == chat.id,
                    db.ChatMember.user_id == user.id,
                )
            ).scalar_one_or_none()
            if member is None:
                return False

            return bool(member.is_whitelisted)
    except Exception as exc:
        logger.error(
            "Failed to check whitelist chat_id=%s user_id=%s: %s",
            chat_id,
            user_id,
            exc,
            exc_info=True,
        )
        return False


def is_service_or_bot_message(message: Message) -> bool:
    """Фильтруем сервисные сообщения и сообщения от ботов."""
    if message.from_user is None:
        return True
    if message.from_user.is_bot:
        return True
    if message.text is None:
        return True
    return False


async def apply_escalation(
    bot: Bot,
    *,
    chat_id: int,
    user,
    strike_count: int | None,
):
    """
    На основе количества страйков применяем санкции:
    1 — предупреждение,
    2 — мут,
    3+ — бан.
    """
    if strike_count is None:
        return

    display_name = user.full_name or user.username or "пользователь"
    mention = f'<a href="tg://user?id={user.id}">{html.escape(display_name)}</a>'

    try:
        if strike_count == MAX_STRIKES_WARN:
            text = (
                f"{mention}, ваше сообщение было расценено как возможный скам.\n"
                "Пожалуйста, не публикуйте подобные предложения. "
                "Повторные нарушения могут привести к ограничениям и блокировке."
            )
            await bot.send_message(chat_id, text, parse_mode="HTML")

        elif strike_count == MAX_STRIKES_MUTE:
            until_date = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=MUTE_HOURS)
            permissions = ChatPermissions(
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
                can_change_info=False,
                can_invite_users=False,
                can_pin_messages=False,
            )
            await bot.restrict_chat_member(
                chat_id,
                user.id,
                permissions=permissions,
                until_date=until_date,
            )
            text = (
                f"{mention} временно ограничен(а) в праве писать в чат на {MUTE_HOURS} ч "
                "за повторные подозрительные сообщения."
            )
            await bot.send_message(chat_id, text, parse_mode="HTML")

        elif strike_count >= MAX_STRIKES_BAN:
            await bot.ban_chat_member(chat_id, user.id)
            text = (
                f"{mention} был(а) удалён(а) из чата за множественные подозрительные сообщения.\n"
                "Если это ошибка, администраторы могут пересмотреть решение вручную."
            )
            await bot.send_message(chat_id, text, parse_mode="HTML")

    except Exception as exc:
        logger.error(
            "Failed to apply escalation (chat_id=%s user_id=%s strikes=%s): %s",
            chat_id,
            user.id,
            strike_count,
            exc,
            exc_info=True,
        )


async def notify_admins_about_scam(
    bot: Bot,
    *,
    message: Message,
    result: LlmModerationResult,
    strike_count: int | None,
    message_db_id: int | None,
):
    """
    Отправляем карточку о скам-сообщении:
    - в локальный админ-чат, привязанный к этому рабочему чату (если настроен)
    - и/или в глобальные админ-чаты из .env (admin_chat_ids)
    """
    from app.db.session import get_session
    from app.db import models as db
    from sqlalchemy import select

    # 1. Берём ТОЛЬКО admin_chat_telegram_id, пока сессия жива
    local_admin_chat_id: int | None = None
    with get_session() as session:
        chat = session.execute(
            select(db.Chat).where(db.Chat.telegram_chat_id == message.chat.id)
        ).scalar_one_or_none()
        if chat is not None:
            local_admin_chat_id = chat.admin_chat_telegram_id

    # 2. Глобальные админ-чаты (суперадмины) из .env
    global_admin_chat_ids = parse_global_admin_chat_ids()

    # 3. Формируем список целей, без дублей
    target_chat_ids: list[int] = []
    if local_admin_chat_id is not None:
        target_chat_ids.append(local_admin_chat_id)
    for cid in global_admin_chat_ids:
        if cid not in target_chat_ids:
            target_chat_ids.append(cid)

    if not target_chat_ids:
        logger.info(
            "No admin chats configured for scam notification: source_chat_id=%s",
            message.chat.id,
        )
        return

    user = message.from_user
    assert user is not None

    chat_title = message.chat.title or "(без названия)"
    user_display = user.full_name or (user.username or f"id {user.id}")

    # Экранируем для HTML
    chat_title_safe = html.escape(chat_title)
    user_display_safe = html.escape(user_display)
    text_safe = html.escape(message.text or "")
    reason_safe = html.escape(result.reason or "")

    strikes = strike_count if strike_count is not None else 1
    db_id_part = (
        f"\n🆔 ID в БД: <code>{message_db_id}</code>"
        if message_db_id is not None
        else ""
    )

    body = (
        "🚨 <b>Обнаружен возможный скам</b>\n\n"
        f"👥 Чат: <b>{chat_title_safe}</b> (<code>{message.chat.id}</code>)\n"
        f"🙍‍♂️ Пользователь: <b>{user_display_safe}</b> "
        f"(<code>{user.id}</code>)\n"
        f"⚠️ Страйков в этом чате: <b>{strikes}</b>"
        f"{db_id_part}\n\n"
        f"🤖 Модель: <code>{settings.openai_model}</code>\n"
        f"🏷 Метка: <b>{result.label}</b> ({result.category})\n"
        f"📊 Уверенность: <b>{result.confidence:.2f}</b>\n"
        f"📝 Причина: {reason_safe}\n\n"
        f"💬 Текст сообщения:\n"
        f"<code>{text_safe}</code>"
    )

    reply_markup = None
    if message_db_id is not None:
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Не скам",
                        callback_data=f"as_not_scam:{message_db_id}",
                    ),
                    InlineKeyboardButton(
                        text="🚫 Точно скам",
                        callback_data=f"as_mark_scam:{message_db_id}",
                    ),
                ]
            ]
        )

    logger.info(
        "Sending scam notification: source_chat_id=%s -> targets=%s",
        message.chat.id,
        target_chat_ids,
    )

    for admin_chat_id in target_chat_ids:
        try:
            await bot.send_message(
                admin_chat_id,
                body,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.error(
                "Failed to send scam notification to admin chat %s: %s",
                admin_chat_id,
                exc,
            )



@router.message()
async def handle_message(message: Message, bot: Bot):
    # Рабочие только группы/супергруппы
    if message.chat.type not in ("group", "supergroup"):
        return

    # Не сканируем админ-чаты (ни глобальные, ни локальные)
    if is_admin_chat_id(message.chat.id):
        return

    if is_service_or_bot_message(message):
        return

    from_user = message.from_user
    assert from_user is not None

    chat_id = message.chat.id
    user_id = from_user.id
    text = message.text or ""

    # Не отправляем в LLM админов и whitelist
    if await is_admin(bot, chat_id, user_id) or await is_whitelisted(chat_id, user_id):
        return

    logger.info(
        "Incoming message for moderation: chat_id=%s user_id=%s text=%r",
        chat_id,
        user_id,
        text,
    )

    # 1. LLM-классификация
    result = await classify_text(text)
    if result is None:
        logger.warning(
            "LLM returned no result, skipping DB save and actions for chat_id=%s user_id=%s",
            chat_id,
            user_id,
        )
        return

    logger.info(
        "LLM result: chat_id=%s user_id=%s label=%s category=%s confidence=%.3f reason=%r",
        chat_id,
        user_id,
        result.label,
        result.category,
        result.confidence,
        result.reason,
    )

    confidence_threshold = settings.default_confidence_threshold
    is_scam_policy = result.label == "SCAM" and result.confidence >= confidence_threshold
    skipped_reason: str | None = None
    if result.label == "SCAM" and not is_scam_policy:
        skipped_reason = "low_confidence"

    new_strike_count: int | None = None
    saved_message_id: int | None = None
    chat_admin_chat_id: int | None = None

    # 2. Сохраняем всё в БД
    with get_session() as session:
        # Чат
        chat = session.execute(
            select(db.Chat).where(db.Chat.telegram_chat_id == chat_id)
        ).scalar_one_or_none()
        if chat is None:
            chat = db.Chat(
                telegram_chat_id=chat_id,
                title=message.chat.title,
                type=message.chat.type,
            )
            session.add(chat)
            session.flush()

        # Пользователь
        user = session.execute(
            select(db.User).where(db.User.telegram_user_id == user_id)
        ).scalar_one_or_none()
        if user is None:
            user = db.User(
                telegram_user_id=user_id,
                username=from_user.username,
                first_name=from_user.first_name,
                last_name=from_user.last_name,
            )
            session.add(user)
            session.flush()
        else:
            user.username = from_user.username
            user.first_name = from_user.first_name
            user.last_name = from_user.last_name

        # ChatMember
        member = session.execute(
            select(db.ChatMember).where(
                db.ChatMember.chat_id == chat.id,
                db.ChatMember.user_id == user.id,
            )
        ).scalar_one_or_none()
        if member is None:
            member = db.ChatMember(chat_id=chat.id, user_id=user.id)
            session.add(member)
            session.flush()

        # Страйки
        if is_scam_policy:
            member.strike_count = (member.strike_count or 0) + 1
            member.last_strike_at = dt.datetime.now(dt.timezone.utc)
            new_strike_count = member.strike_count

        # Сообщение
        msg = db.Message(
            chat_id=chat.id,
            user_id=user.id,
            telegram_message_id=message.message_id,
            text=text,
            model_label=result.label,
            model_category=result.category,
            model_confidence=result.confidence,
            model_reason=result.reason,
            model_raw_json=result.raw_response,
            model_version=settings.openai_model,
            human_label=None,
            human_labeled_at=None,
            human_labeled_by=None,
            is_scam_effective=is_scam_policy,
            skipped_reason=skipped_reason,
        )
        session.add(msg)
        session.flush()
        saved_message_id = msg.id

        chat_admin_chat_id = chat.admin_chat_telegram_id

    # 3. Политика действий в чате
    if is_scam_policy:
        try:
            await message.delete()
            logger.info(
                "Deleted scam message: chat_id=%s user_id=%s strike_count=%s",
                chat_id,
                user_id,
                new_strike_count,
            )
        except Exception as exc:
            logger.error(
                "Failed to delete scam message chat_id=%s user_id=%s: %s",
                chat_id,
                user_id,
                exc,
                exc_info=True,
            )

        await apply_escalation(
            bot,
            chat_id=chat_id,
            user=from_user,
            strike_count=new_strike_count,
        )

        await notify_admins_about_scam(
            bot,
            message=message,
            result=result,
            strike_count=new_strike_count,
            message_db_id=saved_message_id,
        )
