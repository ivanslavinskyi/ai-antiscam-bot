import logging
import datetime as dt
import html
import re
import asyncio

from aiogram import Router, Bot
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatPermissions,
)
from sqlalchemy import select, or_, func

from app.moderation.llm_client import classify_text
from app.moderation.ocr import ocr_image_file_id
from app.moderation.text_normalizer import normalize_message_text, normalize_unicode_fonts
from app.db.session import get_session
from app.db import models as db
from app.config import settings
from app.moderation.schemas import LlmModerationResult
from app.services.chat_health import should_deactivate_chat, deactivate_chat

router = Router(name="messages-router")
logger = logging.getLogger(__name__)

# Пороговые значения для эскалации
MAX_STRIKES_WARN = 1      # 1-й скам — предупреждение
MAX_STRIKES_MUTE = 4      # 2-й скам — временный мут
MAX_STRIKES_BAN = 5       # 3-й и далее — бан
MUTE_HOURS = 24           # длительность мута (часов)

URL_RE = re.compile(r"(https?://\S+|t\.me/\S+|www\.\S+)", re.IGNORECASE)
MENTION_RE = re.compile(r"@\w+", re.UNICODE)
HASHTAG_RE = re.compile(r"#\w+", re.UNICODE)
ALNUM_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]", re.UNICODE)


def is_link_only(text: str) -> bool:
    if not text:
        return False
    if URL_RE.search(text) is None:
        return False
    without_urls = URL_RE.sub(" ", text)
    return re.search(r"[0-9A-Za-zА-Яа-яЁё]", without_urls) is None


def _message_has_image(message: Message) -> bool:
    # photo[] или document с mime image/*
    if getattr(message, "photo", None):
        return True
    doc = getattr(message, "document", None)
    if doc and (doc.mime_type or "").startswith("image/"):
        return True
    return False


def _get_image_file_id(message: Message) -> str | None:
    """Извлекает file_id изображения из сообщения (наибольшее разрешение)."""
    if message.photo:
        return message.photo[-1].file_id
    doc = getattr(message, "document", None)
    if doc and (doc.mime_type or "").startswith("image/"):
        return doc.file_id
    return None


def _strip_caption_noise(text: str) -> str:
    # Убираем URL, @username, #hashtag и знаки пунктуации/символы
    t = URL_RE.sub(" ", text or "")
    t = MENTION_RE.sub(" ", t)
    t = HASHTAG_RE.sub(" ", t)
    # Убираем всё, кроме букв/цифр/пробелов/подчёркивания
    t = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_\s]", " ", t, flags=re.UNICODE)
    t = " ".join(t.split())
    return t


def is_weak_caption(caption: str) -> bool:
    """
    Раньше использовалось для OCR.
    Оставлено как утилита на будущее (сейчас в модерации не применяется).
    """
    c = (caption or "").strip()
    if not c:
        return True

    min_chars = 25
    min_words = 4

    if len(c) < min_chars:
        return True
    if len(c.split()) < min_words:
        return True

    cleaned = _strip_caption_noise(c)
    if not ALNUM_RE.search(cleaned or ""):
        return True

    return False


def parse_global_admin_chat_ids() -> list[int]:
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
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as exc:
        logger.warning(
            "Failed to get chat administrators for chat_id=%s: %s",
            chat_id,
            exc,
        )
        return False
    return user_id in {m.user.id for m in admins}


async def is_whitelisted(chat_id: int, user_id: int) -> bool:
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


def _is_forwarded(message: Message) -> bool:
    """True если сообщение является пересланным (из канала, от пользователя и т.д.)."""
    return any([
        getattr(message, "forward_origin", None) is not None,
        getattr(message, "forward_from", None) is not None,
        getattr(message, "forward_from_chat", None) is not None,
        getattr(message, "forward_date", None) is not None,
    ])


def is_service_or_bot_message(message: Message) -> bool:
    if message.from_user is None:
        # Пересланные сообщения из каналов пропускаем для проверки LLM
        if not _is_forwarded(message):
            return True
    elif message.from_user.is_bot:
        return True

    # Текст может быть в caption (медиа-посты). Изображения без caption тоже пропускаем
    # до handle_message(), там решаем политику (удалить+предупредить).
    has_text = bool((message.text or "").strip())
    has_caption = bool((message.caption or "").strip())
    has_image = _message_has_image(message)

    if not has_text and not has_caption and not has_image:
        return True

    return False


async def _delete_message_later(bot: Bot, chat_id: int, message_id: int, delay_sec: int) -> None:
    """Удаляет сообщение бота через delay_sec секунд. Ошибки не пробрасываем."""
    try:
        await asyncio.sleep(max(0, int(delay_sec)))
        await bot.delete_message(chat_id, message_id)
    except Exception as exc:
        logger.debug(
            "Auto-delete failed (chat_id=%s message_id=%s): %s",
            chat_id,
            message_id,
            exc,
        )


async def send_group_notice(
    bot: Bot,
    *,
    chat_id: int,
    text: str,
    parse_mode: str = "HTML",
) -> None:
    """
    Отправляет уведомление в рабочую группу и (опционально) удаляет его через TTL.
    TTL берём из settings.group_notice_ttl_seconds (0/<=0 — не удаляем).
    """
    ttl = int(getattr(settings, "group_notice_ttl_seconds", 60) or 0)
    sent = await bot.send_message(chat_id, text, parse_mode=parse_mode)
    if ttl > 0:
        asyncio.create_task(_delete_message_later(bot, chat_id, sent.message_id, ttl))


async def apply_escalation(
    bot: Bot,
    *,
    chat_id: int,
    user,
    strike_count: int | None,
):
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
            await send_group_notice(bot, chat_id=chat_id, text=text, parse_mode="HTML")

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
            await send_group_notice(bot, chat_id=chat_id, text=text, parse_mode="HTML")

        elif strike_count >= MAX_STRIKES_BAN:
            await bot.ban_chat_member(chat_id, user.id)
            text = (
                f"{mention} был(а) удалён(а) из чата за множественные подозрительные сообщения.\n"
                "Если это ошибка, администраторы могут пересмотреть решение вручную."
            )
            await send_group_notice(bot, chat_id=chat_id, text=text, parse_mode="HTML")

    except Exception as exc:
        logger.error(
            "Failed to apply escalation (chat_id=%s user_id=%s strikes=%s): %s",
            chat_id,
            user.id,
            strike_count,
            exc,
            exc_info=True,
        )
        reason = should_deactivate_chat(exc)
        if reason:
            deactivate_chat(chat_id, reason)


async def notify_admins_about_scam(
    bot: Bot,
    *,
    message: Message,
    result: LlmModerationResult,
    strike_count: int | None,
    message_db_id: int | None,
    effective_text: str | None = None,
):
    """
    Отправляем карточку о скам-сообщении:
    - в локальный админ-чат, привязанный к этому рабочему чату (если настроен)
    - и/или в глобальные админ-чаты из .env (admin_chat_ids)
    """
    local_admin_chat_id: int | None = None
    with get_session() as session:
        chat = session.execute(
            select(db.Chat).where(db.Chat.telegram_chat_id == message.chat.id)
        ).scalar_one_or_none()
        if chat is not None:
            local_admin_chat_id = chat.admin_chat_telegram_id

    global_admin_chat_ids = parse_global_admin_chat_ids()

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
    text = effective_text or (message.text or message.caption or "").strip()

    chat_title_safe = html.escape(chat_title)
    user_display_safe = html.escape(user_display)
    text_safe = html.escape(text)
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
        f"🙍 Пользователь: <b>{user_display_safe}</b> "
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
        # Добавляем быстрые действия: бан и whitelist по автору (в админ-чате).
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
                ],
                [
                    InlineKeyboardButton(
                        text="🚫 Забанить",
                        callback_data=f"as_u_ban:{user.id}",
                    ),
                    InlineKeyboardButton(
                        text="✅ В whitelist",
                        callback_data=f"as_u_wl:{user.id}",
                    ),
                ],
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


def count_not_scam_credits(session, *, user_db_id: int, limit_n: int) -> int:
    rows = (
        session.execute(
            select(db.Message.id).where(
                db.Message.user_id == user_db_id,
                or_(db.Message.model_label == "OK", db.Message.human_label == "NOT_SCAM"),
            ).limit(limit_n)
        )
        .scalars()
        .all()
    )
    return len(rows)


async def notify_admins_about_auto_whitelist(
    bot: Bot,
    *,
    message: Message,
    credits_count: int,
    threshold: int,
    chat_admin_chat_id: int | None,
):
    target_chat_ids: list[int] = []
    if chat_admin_chat_id is not None:
        target_chat_ids.append(chat_admin_chat_id)
    for cid in parse_global_admin_chat_ids():
        if cid not in target_chat_ids:
            target_chat_ids.append(cid)

    if not target_chat_ids:
        return

    user = message.from_user
    if user is None:
        return

    chat_title = message.chat.title or "(без названия)"
    user_display = user.full_name or (user.username or f"id {user.id}")

    body = (
        f"🟢 Авто-whitelist: достигнут порог {threshold} НЕ СКАМ\n\n"
        f"👥 Чат: <b>{html.escape(chat_title)}</b> (<code>{message.chat.id}</code>)\n"
        f"🙍 Пользователь: <b>{html.escape(user_display)}</b> (<code>{user.id}</code>)\n"
        f"📈 Счёт: <b>{credits_count}</b> / <b>{threshold}</b>\n\n"
        "Основание: учитываются <b>LLM=OK</b> и ручные разметки <b>NOT_SCAM</b>."
    )

    for admin_chat_id in target_chat_ids:
        try:
            await bot.send_message(admin_chat_id, body, parse_mode="HTML")
        except Exception as exc:
            logger.error(
                "Failed to send auto-whitelist notification to admin chat %s: %s",
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

    from_user = message.from_user  # None для пересланных из каналов
    is_forwarded_channel_post = from_user is None

    chat_id = message.chat.id
    user_id = from_user.id if from_user else 0

    # --- ТЗ: не отправляем в LLM некоторые типы сообщений -----------------
    # Reply на пересланные — ПРОВЕРЯЕМ (текст пересланного, страйки к ответившему).
    # Обычные reply — по-прежнему НЕ проверяем.
    is_reply_to_forward = False
    if message.reply_to_message is not None:
        if _is_forwarded(message.reply_to_message):
            is_reply_to_forward = True
        else:
            return  # обычный ответ — пропускаем

    # Админов и whitelist не проверяем (только если есть from_user)
    if not is_forwarded_channel_post:
        if await is_admin(bot, chat_id, user_id) or await is_whitelisted(chat_id, user_id):
            return

    # Для reply на пересланные: извлекаем текст из ПЕРЕСЛАННОГО сообщения (не из ответа).
    source_message = message.reply_to_message if is_reply_to_forward else message

    raw_text = (source_message.text or "").strip()
    raw_caption = (source_message.caption or "").strip()
    has_image = _message_has_image(source_message)

    # --- Обработка изображений: OCR + caption → LLM ---
    if has_image:
        image_file_id = _get_image_file_id(source_message)
        ocr_text = ""
        if image_file_id:
            try:
                ocr_result, ocr_error = await ocr_image_file_id(bot, image_file_id)
                if ocr_result:
                    ocr_text = ocr_result
                    logger.info(
                        "OCR extracted text: chat_id=%s user_id=%s len=%d text=%r",
                        chat_id, user_id, len(ocr_result), ocr_result[:200],
                    )
                elif ocr_error:
                    logger.info(
                        "OCR skipped: chat_id=%s user_id=%s error=%s",
                        chat_id, user_id, ocr_error,
                    )
            except Exception as exc:
                logger.warning(
                    "OCR unexpected error chat_id=%s user_id=%s: %s",
                    chat_id, user_id, exc, exc_info=True,
                )

        # Объединяем caption + OCR-текст
        parts = [p for p in [raw_caption, ocr_text] if p]
        text = "\n".join(parts).strip()

        if not text:
            # Ни caption, ни OCR-текста — удаляем и просим добавить текст
            try:
                await message.delete()
            except Exception as exc:
                logger.error(
                    "Failed to delete image-without-text chat_id=%s user_id=%s: %s",
                    chat_id, user_id, exc,
                )
            warn = (
                "⚠️ Пожалуйста, добавьте текст к изображению, "
                "картинки без текста часто используются для обхода скам-фильтров. "
                "Если это ошибка, сообщите администратору."
            )
            await send_group_notice(bot, chat_id=chat_id, text=warn, parse_mode="HTML")
            return
    else:
        text = (raw_text or raw_caption).strip()
        if not text:
            return

    # Если сообщение — только ссылка(и) без текста
    if is_link_only(text):
        return

    # --- Нормализация текста: раскрытие кастомных эмодзи и Unicode-шрифтов ---
    normalized_text, was_normalized = await normalize_message_text(bot, source_message)
    if was_normalized and normalized_text:
        logger.info(
            "Text normalized from custom emoji / Unicode fonts: "
            "chat_id=%s user_id=%s original=%r normalized=%r",
            chat_id, user_id, text[:200], normalized_text[:200],
        )
        text = normalized_text
    else:
        # Даже без custom_emoji, нормализуем Unicode-шрифты в обычном тексте
        text = normalize_unicode_fonts(text)

    logger.info(
        "Incoming message for moderation: chat_id=%s user_id=%s text=%r",
        chat_id,
        user_id,
        text,
    )

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

    # --- Для пересланных постов из каналов (from_user=None): ---
    # Проверяем текст через LLM, удаляем если скам, уведомляем админов,
    # но НЕ создаём записи user/member и НЕ применяем эскалацию (некому).
    if is_forwarded_channel_post:
        if is_scam_policy:
            try:
                await message.delete()
            except Exception as exc:
                logger.error(
                    "Failed to delete forwarded scam chat_id=%s: %s", chat_id, exc,
                )
            await notify_admins_about_scam(
                bot,
                message=message,
                result=result,
                strike_count=None,
                message_db_id=None,
                effective_text=text,
            )
        return

    # --- Обычные сообщения от пользователей ---
    new_strike_count: int | None = None
    saved_message_id: int | None = None
    chat_admin_chat_id: int | None = None

    auto_whitelist_threshold: int = int(settings.auto_whitelist_threshold)
    newly_whitelisted: bool = False
    credits_count: int | None = None

    with get_session() as session:
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

        if is_scam_policy:
            member.strike_count = (member.strike_count or 0) + 1
            member.last_strike_at = dt.datetime.now(dt.timezone.utc)
            new_strike_count = member.strike_count

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

        if result.label == "OK":
            credits = count_not_scam_credits(session, user_db_id=user.id, limit_n=auto_whitelist_threshold)
            credits_count = credits
            if credits >= auto_whitelist_threshold and not user.is_global_whitelisted:
                user.is_global_whitelisted = True
                newly_whitelisted = True

    if newly_whitelisted:
        await notify_admins_about_auto_whitelist(
            bot,
            message=message,
            credits_count=credits_count or auto_whitelist_threshold,
            threshold=auto_whitelist_threshold,
            chat_admin_chat_id=chat_admin_chat_id,
        )

    if is_scam_policy:
        try:
            await message.delete()
        except Exception as exc:
            logger.error("Failed to delete scam message chat_id=%s user_id=%s: %s", chat_id, user_id, exc)

        # Если это reply на пересланное — удаляем и пересланное сообщение
        if is_reply_to_forward:
            try:
                await message.reply_to_message.delete()
            except Exception as exc:
                logger.error("Failed to delete forwarded scam (reply source) chat_id=%s: %s", chat_id, exc)

        await apply_escalation(bot, chat_id=chat_id, user=from_user, strike_count=new_strike_count)

        await notify_admins_about_scam(
            bot,
            message=message,
            result=result,
            strike_count=new_strike_count,
            message_db_id=saved_message_id,
            effective_text=text,
        )