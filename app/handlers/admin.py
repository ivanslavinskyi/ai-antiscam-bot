import logging
import html
import datetime as dt
import asyncio

from typing import List

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatPermissions,
)
from sqlalchemy import select, func, or_, update

from app.config import settings
from app.db.session import get_session
from app.db import models as db
from app.services.chat_health import should_deactivate_chat, deactivate_chat, reactivate_chat

logger = logging.getLogger(__name__)

router = Router(name="admin")

# Пауза между массовыми действиями по чатам, чтобы снизить риск flood-limit.
_ADMIN_ACTION_SLEEP_SEC = 0.05


def _is_command_message(message: Message) -> bool:
    """True, если сообщение является командой (bot_command)."""
    if not message.text:
        return False
    if not message.entities:
        return False
    ent = message.entities[0]
    return ent.type == "bot_command" and ent.offset == 0


def parse_global_admin_chat_ids() -> List[int]:
    """Разбираем admin_chat_ids из .env в список int."""
    raw = settings.admin_chat_ids
    if not raw:
        return []
    ids: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            logger.warning("Invalid admin_chat_ids entry in .env: %r", part)
    return ids


def _is_admin_chat(session, chat_id: int) -> bool:
    """Админ-чат, если:
    - он указан как глобальный в .env (admin_chat_ids), или
    - к нему привязан хотя бы один рабочий чат (admin_chat_telegram_id).
    """
    if chat_id in parse_global_admin_chat_ids():
        return True
    exists = session.execute(
        select(db.Chat.id).where(db.Chat.admin_chat_telegram_id == chat_id)
    ).first()
    return exists is not None


def _require_admin_chat(message: Message) -> bool:
    """Сторож: админ-команды должны выполняться только в админ-чатах."""
    try:
        with get_session() as session:
            return _is_admin_chat(session, message.chat.id)
    except Exception as exc:
        logger.error("Failed to check admin chat: %s", exc, exc_info=True)
        return False


def _get_managed_chats(session, admin_chat_id: int) -> List[db.Chat]:
    """Список рабочих чатов, для которых данный чат является админ-чатом."""
    return (
        session.execute(
            select(db.Chat).where(
                db.Chat.admin_chat_telegram_id == admin_chat_id,
                db.Chat.is_active == True,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )


# ---------------------- NEW (для глобальных админ-чатов) ----------------------

def _is_global_admin_chat_id(admin_chat_id: int) -> bool:
    """True, если чат указан в admin_chat_ids (.env)."""
    return admin_chat_id in parse_global_admin_chat_ids()


def _get_target_chats_for_admin(session, admin_chat_id: int) -> List[db.Chat]:
    """Какие рабочие чаты считать подконтрольными для данного админ-чата.

    - Глобальный админ-чат: ВСЕ группы/супергруппы, которые есть в БД.
    - Локальный админ-чат: только чаты, привязанные через admin_chat_telegram_id.
    """
    if _is_global_admin_chat_id(admin_chat_id):
        return (
            session.execute(
                select(db.Chat).where(
                    db.Chat.type.in_(["group", "supergroup"]),
                    db.Chat.is_active == True,  # noqa: E712
                )
            )
            .scalars()
            .all()
        )
    return _get_managed_chats(session, admin_chat_id)

# -----------------------------------------------------------------------------


def _get_all_group_chats(session) -> list[dict]:
    """Список всех подконтрольных АКТИВНЫХ групп (из таблицы chats) как примитивы."""
    rows = session.execute(
        select(db.Chat.id, db.Chat.telegram_chat_id).where(
            db.Chat.type.in_(["group", "supergroup"]),
            db.Chat.is_active == True,  # noqa: E712
        )
    ).all()
    return [{"id": r[0], "telegram_chat_id": r[1]} for r in rows]


def _get_or_create_user_by_telegram_id(
    session,
    *,
    telegram_user_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> db.User:
    user = (
        session.execute(
            select(db.User).where(db.User.telegram_user_id == telegram_user_id)
        )
        .scalars()
        .first()
    )
    if user is None:
        user = db.User(
            telegram_user_id=telegram_user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            is_global_whitelisted=False,
        )
        session.add(user)
        session.flush()
    else:
        if username is not None:
            user.username = username
        if first_name is not None:
            user.first_name = first_name
        if last_name is not None:
            user.last_name = last_name
    return user


def _build_user_action_keyboard(tg_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Внести в whitelist",
                    callback_data=f"as_u_wl:{tg_user_id}",
                ),
                InlineKeyboardButton(
                    text="Обнулить страйки",
                    callback_data=f"as_u_rs:{tg_user_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Разбанить",
                    callback_data=f"as_u_unban:{tg_user_id}",
                ),
                InlineKeyboardButton(
                    text="Забанить",
                    callback_data=f"as_u_ban:{tg_user_id}",
                ),
            ],
        ]
    )


def _parse_single_user_id_arg(text: str | None) -> int | None:
    if not text:
        return None
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    raw = parts[1].strip()
    try:
        return int(raw)
    except ValueError:
        return None


def _permissions_allow_all() -> ChatPermissions:
    """Размут: включаем отправку сообщений/медиа и т.п."""
    return ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_change_info=False,
        can_invite_users=True,
        can_pin_messages=False,
    )


async def _mass_unban_and_unmute(bot: Bot, tg_user_id: int) -> tuple[int, int, list[str]]:
    ok = 0
    fail = 0
    errors: list[str] = []
    perms = _permissions_allow_all()

    with get_session() as session:
        group_chats = _get_all_group_chats(session)

    for chat in group_chats:
        tg_chat_id = chat["telegram_chat_id"]
        try:
            await bot.unban_chat_member(tg_chat_id, tg_user_id)
            await bot.restrict_chat_member(tg_chat_id, tg_user_id, permissions=perms)
            ok += 1
        except Exception as exc:
            fail += 1
            if len(errors) < 5:
                errors.append(f"• <code>{tg_chat_id}</code>: {html.escape(str(exc))}")
            reason = should_deactivate_chat(exc)
            if reason:
                deactivate_chat(tg_chat_id, reason)
        await asyncio.sleep(_ADMIN_ACTION_SLEEP_SEC)

    return ok, fail, errors


async def _mass_unmute(bot: Bot, tg_user_id: int) -> tuple[int, int, list[str]]:
    ok = 0
    fail = 0
    errors: list[str] = []
    perms = _permissions_allow_all()

    with get_session() as session:
        group_chats = _get_all_group_chats(session)

    for chat in group_chats:
        tg_chat_id = chat["telegram_chat_id"]
        try:
            await bot.restrict_chat_member(tg_chat_id, tg_user_id, permissions=perms)
            ok += 1
        except Exception as exc:
            fail += 1
            if len(errors) < 5:
                errors.append(f"• <code>{tg_chat_id}</code>: {html.escape(str(exc))}")
            reason = should_deactivate_chat(exc)
            if reason:
                deactivate_chat(tg_chat_id, reason)
        await asyncio.sleep(_ADMIN_ACTION_SLEEP_SEC)

    return ok, fail, errors


async def _mass_ban(bot: Bot, tg_user_id: int) -> tuple[int, int, list[str]]:
    ok = 0
    fail = 0
    errors: list[str] = []

    with get_session() as session:
        group_chats = _get_all_group_chats(session)

    for chat in group_chats:
        tg_chat_id = chat["telegram_chat_id"]
        try:
            await bot.ban_chat_member(tg_chat_id, tg_user_id)
            ok += 1
        except Exception as exc:
            fail += 1
            if len(errors) < 5:
                errors.append(f"• <code>{tg_chat_id}</code>: {html.escape(str(exc))}")
            reason = should_deactivate_chat(exc)
            if reason:
                deactivate_chat(tg_chat_id, reason)
        await asyncio.sleep(_ADMIN_ACTION_SLEEP_SEC)

    return ok, fail, errors


def _reset_strikes_in_db(tg_user_id: int) -> int:
    """Обнуляем страйки по существующим membership во всех группах. Возвращаем rowcount."""
    with get_session() as session:
        user = _get_or_create_user_by_telegram_id(session, telegram_user_id=tg_user_id)
        group_chat_rows = _get_all_group_chats(session)
        group_chat_ids = [r["id"] for r in group_chat_rows]

        res = session.execute(
            update(db.ChatMember)
            .where(
                db.ChatMember.user_id == user.id,
                db.ChatMember.chat_id.in_(group_chat_ids),
            )
            .values(strike_count=0, last_strike_at=None)
        )
        session.commit()
        return int(getattr(res, "rowcount", 0) or 0)


def _set_global_whitelist(tg_user_id: int) -> bool:
    """Ставит global whitelist. Возвращает True если изменилось (т.е. был не в WL)."""
    with get_session() as session:
        user = _get_or_create_user_by_telegram_id(session, telegram_user_id=tg_user_id)
        was = bool(user.is_global_whitelisted)
        user.is_global_whitelisted = True
        session.commit()
        return not was


# ---------------------------------------------------------------------------
# Быстрые команды в админ-чате по USER_ID
# ---------------------------------------------------------------------------

@router.message(Command("as_wl"))
async def cmd_as_wl(message: Message):
    if not _require_admin_chat(message):
        await message.reply("Команда доступна только в админ-чате.")
        return

    tg_user_id = _parse_single_user_id_arg(message.text)
    if tg_user_id is None:
        await message.reply("Использование: <code>/as_wl USER_ID</code>", parse_mode="HTML")
        return

    changed = _set_global_whitelist(tg_user_id)
    await message.reply(
        (
            f"✅ Global whitelist для <code>{tg_user_id}</code> включён.\n"
            + ("(пользователь был добавлен)" if changed else "(уже был в whitelist)")
        ),
        parse_mode="HTML",
    )


@router.message(Command("as_rs"))
async def cmd_as_rs(message: Message):
    if not _require_admin_chat(message):
        await message.reply("Команда доступна только в админ-чате.")
        return

    tg_user_id = _parse_single_user_id_arg(message.text)
    if tg_user_id is None:
        await message.reply("Использование: <code>/as_rs USER_ID</code>", parse_mode="HTML")
        return

    updated = _reset_strikes_in_db(tg_user_id)
    await message.reply(
        f"✅ Страйки обнулены для <code>{tg_user_id}</code> (membership обновлено: <b>{updated}</b>).",
        parse_mode="HTML",
    )


@router.message(Command("as_unban"))
async def cmd_as_unban(message: Message, bot: Bot):
    if not _require_admin_chat(message):
        await message.reply("Команда доступна только в админ-чате.")
        return

    tg_user_id = _parse_single_user_id_arg(message.text)
    if tg_user_id is None:
        await message.reply("Использование: <code>/as_unban USER_ID</code>", parse_mode="HTML")
        return

    ok, fail, errors = await _mass_unban_and_unmute(bot, tg_user_id)
    lines = [
        f"✅ Разбан + размут для <code>{tg_user_id}</code> во всех группах.",
        f"Успешно: <b>{ok}</b>",
        f"Ошибок: <b>{fail}</b>",
    ]
    if errors:
        lines.append("")
        lines.append("Первые ошибки:")
        lines.extend(errors)
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("as_unmute"))
async def cmd_as_unmute(message: Message, bot: Bot):
    if not _require_admin_chat(message):
        await message.reply("Команда доступна только в админ-чате.")
        return

    tg_user_id = _parse_single_user_id_arg(message.text)
    if tg_user_id is None:
        await message.reply("Использование: <code>/as_unmute USER_ID</code>", parse_mode="HTML")
        return

    ok, fail, errors = await _mass_unmute(bot, tg_user_id)
    lines = [
        f"✅ Размут для <code>{tg_user_id}</code> во всех группах.",
        f"Успешно: <b>{ok}</b>",
        f"Ошибок: <b>{fail}</b>",
    ]
    if errors:
        lines.append("")
        lines.append("Первые ошибки:")
        lines.extend(errors)
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("as_ban"))
async def cmd_as_ban(message: Message, bot: Bot):
    if not _require_admin_chat(message):
        await message.reply("Команда доступна только в админ-чате.")
        return

    tg_user_id = _parse_single_user_id_arg(message.text)
    if tg_user_id is None:
        await message.reply("Использование: <code>/as_ban USER_ID</code>", parse_mode="HTML")
        return

    ok, fail, errors = await _mass_ban(bot, tg_user_id)
    lines = [
        f"🚫 Бан для <code>{tg_user_id}</code> во всех группах.",
        f"Успешно: <b>{ok}</b>",
        f"Ошибок: <b>{fail}</b>",
    ]
    if errors:
        lines.append("")
        lines.append("Первые ошибки:")
        lines.extend(errors)
    await message.reply("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /as_user USER_ID — диагностика пользователя
# ---------------------------------------------------------------------------

@router.message(Command("as_user"))
async def cmd_as_user(message: Message):
    if not _require_admin_chat(message):
        await message.reply("Команда доступна только в админ-чате.")
        return

    tg_user_id = _parse_single_user_id_arg(message.text)
    if tg_user_id is None:
        await message.reply("Использование: <code>/as_user USER_ID</code>", parse_mode="HTML")
        return

    try:
        # ВАЖНО: внутри сессии достаём только примитивы, ORM-объекты наружу не выносим
        with get_session() as session:
            user: db.User | None = (
                session.execute(
                    select(db.User).where(db.User.telegram_user_id == tg_user_id)
                )
                .scalars()
                .first()
            )

            if user is None:
                await message.reply(
                    f"Пользователь <code>{tg_user_id}</code> не найден в БД.\n"
                    "Это значит, что бот пока не видел его сообщений в подконтрольных чатах.",
                    parse_mode="HTML",
                )
                return

            # Снимки полей пользователя (примитивы!)
            user_username = user.username
            user_first_name = user.first_name
            user_last_name = user.last_name
            user_is_wl = bool(user.is_global_whitelisted)
            user_db_id = user.id

            strike_rows = session.execute(
                select(
                    db.Chat.telegram_chat_id,
                    db.Chat.title,
                    db.ChatMember.strike_count,
                    db.ChatMember.last_strike_at,
                )
                .join(db.ChatMember, db.ChatMember.chat_id == db.Chat.id)
                .where(db.ChatMember.user_id == user_db_id)
                .order_by(db.ChatMember.strike_count.desc())
            ).all()

            msg_rows = session.execute(
                select(
                    db.Message.created_at,
                    db.Chat.title,
                    db.Chat.telegram_chat_id,
                    db.Message.text,
                    db.Message.model_label,
                    db.Message.model_category,
                    db.Message.model_confidence,
                    db.Message.human_label,
                    db.Message.skipped_reason,
                    db.Message.is_scam_effective,
                )
                .join(db.Chat, db.Message.chat_id == db.Chat.id)
                .where(db.Message.user_id == user_db_id)
                .order_by(db.Message.created_at.desc())
                .limit(5)
            ).all()

        # Дальше — уже без сессии, только с примитивами
        display_name = user_username or user_first_name or user_last_name or "—"
        display_name = html.escape(display_name)

        lines: list[str] = []
        lines.append("👤 <b>Профиль пользователя</b>")
        lines.append(f"ID: <code>{tg_user_id}</code>")
        lines.append(f"Имя/username: <b>{display_name}</b>")
        lines.append(f"Global whitelist: <b>{'ДА' if user_is_wl else 'НЕТ'}</b>")
        lines.append("")

        lines.append("⚠️ <b>Страйки по чатам</b>")
        if not strike_rows:
            lines.append("— нет данных (membership не найден)")
        else:
            for tg_chat_id, title, strikes, last_strike_at in strike_rows[:15]:
                title = html.escape(title or "(без названия)")
                strikes = strikes or 0
                last = "—"
                if last_strike_at:
                    try:
                        last = last_strike_at.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        last = str(last_strike_at)
                lines.append(
                    f"• <b>{title}</b> (<code>{tg_chat_id}</code>): "
                    f"<b>{strikes}</b> (последний: {html.escape(last)})"
                )
            if len(strike_rows) > 15:
                lines.append(f"… и ещё {len(strike_rows) - 15} чатов.")
        lines.append("")

        lines.append("📝 <b>Последние сообщения</b>")
        if not msg_rows:
            lines.append("— нет сообщений в БД")
        else:
            for (
                created_at,
                chat_title,
                tg_chat_id,
                text,
                model_label,
                model_category,
                model_confidence,
                human_label,
                skipped_reason,
                is_scam_effective,
            ) in msg_rows:
                ts = created_at.strftime("%Y-%m-%d %H:%M") if created_at else "—"
                chat_title = html.escape(chat_title or "(без названия)")
                short = (text or "").replace("\n", " ").strip()
                if len(short) > 140:
                    short = short[:137] + "..."
                short = html.escape(short)

                ml = html.escape(model_label or "—")
                mc = html.escape(model_category or "—")
                conf = f"{model_confidence:.2f}" if isinstance(model_confidence, (int, float)) else "—"
                hl = html.escape(human_label or "—")
                sk = html.escape(skipped_reason or "—")
                eff = "ДА" if is_scam_effective else "НЕТ"

                lines.append(
                    f"• [{ts}] <b>{chat_title}</b> (<code>{tg_chat_id}</code>)\n"
                    f"  LLM: <b>{ml}</b> ({mc}) conf=<b>{conf}</b> | human=<b>{hl}</b> | "
                    f"effective=<b>{eff}</b> | skipped=<b>{sk}</b>\n"
                    f"  <code>{short}</code>"
                )

        await message.reply("\n".join(lines), parse_mode="HTML")

    except Exception as exc:
        logger.error("as_user failed for %s: %s", tg_user_id, exc, exc_info=True)
        await message.reply(
            "⚠️ /as_user упала с ошибкой:\n"
            f"<code>{html.escape(type(exc).__name__)}</code>: <code>{html.escape(str(exc))}</code>\n\n"
            "Stacktrace есть в логах.",
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# Справка/статусы/прочее
# ---------------------------------------------------------------------------

@router.message(Command("help", "as_help"))
async def cmd_as_help(message: Message):
    text = (
        "ℹ️ <b>Справка по анти-скам боту</b>\n\n"
        "<b>Быстрые команды в админ-чате по USER_ID:</b>\n"
        "• <code>/as_user USER_ID</code> — профиль/страйки/последние сообщения\n"
        "• <code>/as_wl USER_ID</code> — добавить в global whitelist\n"
        "• <code>/as_rs USER_ID</code> — обнулить страйки (в БД)\n"
        "• <code>/as_unban USER_ID</code> — разбан + размут во всех группах\n"
        "• <code>/as_unmute USER_ID</code> — только размут во всех группах\n"
        "• <code>/as_ban USER_ID</code> — забанить во всех группах\n\n"
        "<b>Команды:</b>\n"
        "• <code>/as_set_admin_chat &lt;id_админ-чата&gt;</code>\n"
        "• <code>/as_status</code>\n"
        "• <code>/as_stats</code>\n"
        "• <code>/as_recent</code>\n"
        "• <code>/as_deactivated</code> — список деактивированных чатов\n"
        "• <code>/as_reactivate CHAT_ID</code> — реактивировать чат\n\n"
        "<b>Кнопки под карточками:</b>\n"
        "• ✅ Не скам\n"
        "• 🚫 Точно скам\n"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("as_status"))
async def cmd_as_status(message: Message, bot: Bot):
    chat_id = message.chat.id
    chat_type = message.chat.type
    global_admin_ids = parse_global_admin_chat_ids()

    # Снимаем примитивы ВНУТРИ сессии, чтобы не было DetachedInstanceError
    with get_session() as session:
        working_chat: db.Chat | None = (
            session.execute(select(db.Chat).where(db.Chat.telegram_chat_id == chat_id))
            .scalars()
            .first()
        )

        # !!! CHANGED: глобальный админ-чат -> все группы в БД
        managed_chats = _get_target_chats_for_admin(session, chat_id)

        working_chat_db_id = working_chat.id if working_chat is not None else None
        working_chat_admin_chat_id = (
            working_chat.admin_chat_telegram_id if working_chat is not None else None
        )

        managed_chats_snapshot = [
            {"telegram_chat_id": c.telegram_chat_id, "title": c.title} for c in managed_chats
        ]

        msgs_total = msgs_scam_model = msgs_scam_human = msgs_not_scam_human = 0
        if working_chat_db_id is not None:
            msgs_total = (
                session.execute(
                    select(func.count(db.Message.id)).where(db.Message.chat_id == working_chat_db_id)
                ).scalar_one()
                or 0
            )
            msgs_scam_model = (
                session.execute(
                    select(func.count(db.Message.id)).where(
                        db.Message.chat_id == working_chat_db_id,
                        db.Message.model_label == "SCAM",
                    )
                ).scalar_one()
                or 0
            )
            msgs_scam_human = (
                session.execute(
                    select(func.count(db.Message.id)).where(
                        db.Message.chat_id == working_chat_db_id,
                        db.Message.human_label == "SCAM",
                    )
                ).scalar_one()
                or 0
            )
            msgs_not_scam_human = (
                session.execute(
                    select(func.count(db.Message.id)).where(
                        db.Message.chat_id == working_chat_db_id,
                        db.Message.human_label == "NOT_SCAM",
                    )
                ).scalar_one()
                or 0
            )

    if chat_type in ("group", "supergroup") and working_chat_db_id is not None:
        if message.from_user is None:
            await message.answer("Не удалось определить отправителя команды.")
            return

        member = await bot.get_chat_member(chat_id, message.from_user.id)
        if member.status not in ("administrator", "creator"):
            await message.answer("В рабочих чатах команды доступны только администраторам.")
            return

        admin_part = (
            f"<code>{working_chat_admin_chat_id}</code>"
            if working_chat_admin_chat_id is not None
            else "не привязан"
        )

        lines: list[str] = []
        lines.append("ℹ️ <b>Статус рабочего чата</b>")
        lines.append(f"Чат: <b>{html.escape(message.chat.title or '(без названия)')}</b>")
        lines.append(f"ID: <code>{chat_id}</code>")
        lines.append("")
        lines.append(f"Админ-чат для уведомлений: {admin_part}")
        lines.append("")
        lines.append("📊 <b>Локальная статистика:</b>")
        lines.append(f"• Проверено сообщений: <b>{msgs_total}</b>")
        lines.append(f"• Скам по модели: <b>{msgs_scam_model}</b>")
        lines.append(f"• Скам по решению админов: <b>{msgs_scam_human}</b>")
        lines.append(f"• Помечено как НЕ скам: <b>{msgs_not_scam_human}</b>")
        lines.append("")
        lines.append("Изменить админ-чат: <code>/as_set_admin_chat &lt;id&gt;</code>")

        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    if managed_chats_snapshot:
        lines = []
        lines.append("ℹ️ <b>Статус админ-чата</b>")
        lines.append(f"Чат: <b>{html.escape(message.chat.title or '(без названия)')}</b>")
        lines.append(f"ID: <code>{chat_id}</code>")
        lines.append("")
        is_global = chat_id in global_admin_ids
        lines.append("Роль: <b>глобальный админ-чат</b>." if is_global else "Роль: <b>локальный админ-чат</b>.")
        lines.append("")
        lines.append(f"Подконтрольных рабочих чатов: <b>{len(managed_chats_snapshot)}</b>")
        for c in managed_chats_snapshot[:10]:
            title = html.escape(c["title"] or "(без названия)")
            lines.append(f"• <b>{title}</b> (<code>{c['telegram_chat_id']}</code>)")
        if len(managed_chats_snapshot) > 10:
            lines.append(f"… и ещё {len(managed_chats_snapshot) - 10} чатов.")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    is_global = chat_id in global_admin_ids
    lines = []
    lines.append("ℹ️ <b>Статус чата</b>")
    lines.append(f"ID: <code>{chat_id}</code>")
    lines.append("")
    if is_global:
        lines.append("Этот чат указан как глобальный админ-чат, но в базе пока нет ни одного рабочего чата (group/supergroup).")
    else:
        lines.append("Этот чат не привязан как рабочий и не используется как админ-чат.")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("as_set_admin_chat"))
async def cmd_as_set_admin_chat(message: Message, bot: Bot):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Эту команду нужно выполнять в групповом чате, не в личке.")
        return

    if message.from_user is None:
        await message.answer("Не удалось определить отправителя команды.")
        return

    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ("administrator", "creator"):
        await message.answer("Только администраторы этого чата могут менять привязку.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /as_set_admin_chat -1001234567890")
        return

    try:
        admin_chat_id = int(parts[1].strip())
    except ValueError:
        await message.answer("ID админ-чата должен быть числом.")
        return

    with get_session() as session:
        chat = (
            session.execute(select(db.Chat).where(db.Chat.telegram_chat_id == message.chat.id))
            .scalars()
            .first()
        )
        if chat is None:
            chat = db.Chat(
                telegram_chat_id=message.chat.id,
                title=message.chat.title,
                type=message.chat.type,
                admin_chat_telegram_id=admin_chat_id,
            )
            session.add(chat)
        else:
            chat.admin_chat_telegram_id = admin_chat_id
        session.commit()

    await message.answer(
        f"Для этого чата теперь используется админ-чат: <code>{admin_chat_id}</code>",
        parse_mode="HTML",
    )


@router.message(Command("as_stats"))
async def cmd_as_stats(message: Message):
    admin_chat_id = message.chat.id

    with get_session() as session:
        # !!! CHANGED: глобальный админ-чат -> все группы в БД
        chats = _get_target_chats_for_admin(session, admin_chat_id)
        if not chats:
            if _is_global_admin_chat_id(admin_chat_id):
                await message.answer(
                    "В базе пока нет ни одного рабочего чата (group/supergroup).",
                    parse_mode="HTML",
                )
            else:
                await message.answer(
                    "Этот чат пока не привязан ни к одному рабочему чату.\n"
                    f"Выполни в рабочем чате: <code>/as_set_admin_chat {admin_chat_id}</code>",
                    parse_mode="HTML",
                )
            return

        chat_ids = [c.id for c in chats]

        total_messages = (
            session.execute(select(func.count(db.Message.id)).where(db.Message.chat_id.in_(chat_ids))).scalar_one()
            or 0
        )
        total_scam_model = (
            session.execute(
                select(func.count(db.Message.id)).where(
                    db.Message.chat_id.in_(chat_ids),
                    db.Message.model_label == "SCAM",
                )
            ).scalar_one()
            or 0
        )
        total_scam_human = (
            session.execute(
                select(func.count(db.Message.id)).where(
                    db.Message.chat_id.in_(chat_ids),
                    db.Message.human_label == "SCAM",
                )
            ).scalar_one()
            or 0
        )
        total_not_scam_human = (
            session.execute(
                select(func.count(db.Message.id)).where(
                    db.Message.chat_id.in_(chat_ids),
                    db.Message.human_label == "NOT_SCAM",
                )
            ).scalar_one()
            or 0
        )

    lines: list[str] = []
    lines.append("📊 <b>Статистика анти-скам бота</b>")
    lines.append(f"Всего проверенных сообщений: <b>{total_messages}</b>")
    lines.append(f"Скам по модели: <b>{total_scam_model}</b>")
    lines.append(f"Скам по решению админов: <b>{total_scam_human}</b>")
    lines.append(f"Помечено как НЕ скам: <b>{total_not_scam_human}</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("as_recent"))
async def cmd_as_recent(message: Message):
    admin_chat_id = message.chat.id

    parts = (message.text or "").split(maxsplit=1)
    limit = 10
    if len(parts) == 2:
        try:
            limit = max(1, min(50, int(parts[1].strip())))
        except ValueError:
            pass

    with get_session() as session:
        # !!! CHANGED: глобальный админ-чат -> все группы в БД
        chats = _get_target_chats_for_admin(session, admin_chat_id)
        if not chats:
            if _is_global_admin_chat_id(admin_chat_id):
                await message.answer(
                    "В базе пока нет ни одного рабочего чата (group/supergroup).",
                    parse_mode="HTML",
                )
            else:
                await message.answer(
                    "Этот чат пока не привязан ни к одному рабочему чату.\n"
                    f"Выполни в рабочем чате: <code>/as_set_admin_chat {admin_chat_id}</code>",
                    parse_mode="HTML",
                )
            return

        chat_ids = [c.id for c in chats]

        stmt = (
            select(db.Message, db.User, db.Chat)
            .join(db.User, db.Message.user_id == db.User.id)
            .join(db.Chat, db.Message.chat_id == db.Chat.id)
            .where(
                db.Message.chat_id.in_(chat_ids),
                or_(db.Message.model_label == "SCAM", db.Message.human_label == "SCAM"),
            )
            .order_by(db.Message.created_at.desc())
            .limit(limit)
        )
        db_rows = session.execute(stmt).all()

    if not db_rows:
        await message.answer("Пока нет ни одного скам-сообщения в этих чатах.")
        return

    lines: list[str] = []
    lines.append(f"🕒 Последние {len(db_rows)} скам-сообщений:")
    for msg_obj, user_obj, chat_obj in db_rows:
        ts = msg_obj.created_at.strftime("%Y-%m-%d %H:%M") if msg_obj.created_at else "—"
        chat_title = html.escape(chat_obj.title or "(без названия)")
        user_name = user_obj.username or user_obj.first_name or f"id {user_obj.telegram_user_id}"
        user_name = html.escape(user_name)
        text = (msg_obj.text or "").replace("\n", " ")
        if len(text) > 120:
            text = text[:117] + "..."
        text = html.escape(text)
        lines.append(f"• [{ts}] <b>{chat_title}</b> — {user_name}: <code>{text}</code>")

    await message.answer("\n".join(lines), parse_mode="HTML")


# --------------------- Дальше идёт НЕИЗМЕНЁННАЯ логика ---------------------
# ВАЖНО: ниже оставлена вся логика карточек, callback’ов, форвард-панелей и т.д.
# Я НЕ урезал её. Если хочешь — я могу дополнительно проставить маркеры "UNCHANGED".

# (Оставлено полностью как в admin-bak.py — без сокращений)
# ---------------------------------------------------------------------------

def _parse_message_id_from_callback(data: str) -> int | None:
    try:
        _, id_str = data.split(":", 1)
        return int(id_str)
    except Exception:
        return None


def _get_or_create_user_by_telegram(session, tg_user) -> db.User:
    user = (
        session.execute(select(db.User).where(db.User.telegram_user_id == tg_user.id))
        .scalars()
        .first()
    )
    if user is None:
        user = db.User(
            telegram_user_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
            is_global_whitelisted=False,
        )
        session.add(user)
        session.flush()
    else:
        user.username = tg_user.username
        user.first_name = tg_user.first_name
        user.last_name = tg_user.last_name
    return user


@router.callback_query(F.data.startswith("as_not_scam:"))
async def cb_not_scam(callback: CallbackQuery):
    message_db_id = _parse_message_id_from_callback(callback.data or "")
    if message_db_id is None:
        await callback.answer("Некорректный формат callback_data.", show_alert=True)
        return

    admin_chat_id = callback.message.chat.id
    global_admin_ids = parse_global_admin_chat_ids()

    auto_whitelist_threshold: int = int(getattr(settings, "auto_whitelist_threshold", 5))
    newly_whitelisted = False

    with get_session() as session:
        msg_obj: db.Message | None = session.get(db.Message, message_db_id)
        if msg_obj is None:
            await callback.answer("Запись уже не найдена в базе.", show_alert=True)
            return

        chat_obj: db.Chat | None = (
            session.execute(select(db.Chat).where(db.Chat.id == msg_obj.chat_id))
            .scalars()
            .first()
        )
        if chat_obj is None:
            await callback.answer("Связанный чат не найден в базе.", show_alert=True)
            return

        is_global_admin_chat = admin_chat_id in global_admin_ids
        if not is_global_admin_chat and chat_obj.admin_chat_telegram_id != admin_chat_id:
            await callback.answer("Нет доступа к этой записи.", show_alert=True)
            return

        admin_user = _get_or_create_user_by_telegram(session, callback.from_user)
        msg_obj.human_label = "NOT_SCAM"
        msg_obj.human_labeled_at = dt.datetime.now(dt.timezone.utc)
        msg_obj.human_labeled_by = admin_user.id

        user_obj: db.User | None = session.get(db.User, msg_obj.user_id)
        if user_obj is not None and not user_obj.is_global_whitelisted:
            rows = (
                session.execute(
                    select(db.Message.id).where(
                        db.Message.user_id == user_obj.id,
                        or_(
                            db.Message.model_label == "OK",
                            db.Message.human_label == "NOT_SCAM",
                        ),
                    ).limit(auto_whitelist_threshold)
                )
                .scalars()
                .all()
            )
            if len(rows) >= auto_whitelist_threshold:
                user_obj.is_global_whitelisted = True
                newly_whitelisted = True

        session.commit()

    try:
        old_text = callback.message.text or ""
        marker = "\n\n👮 Решение админа:"
        base_text = old_text.split(marker, 1)[0] if marker in old_text else old_text
        extra = (
            f"\n🟢 Авто-whitelist: достигнут порог {auto_whitelist_threshold} НЕ СКАМ"
            if newly_whitelisted
            else ""
        )
        new_text = base_text + "\n\n👮 Решение админа: <b>НЕ СКАМ</b>" + extra
        await callback.message.edit_text(new_text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Failed to edit admin card text: %s", exc)

    await callback.answer("Помечено как НЕ скам.")


@router.callback_query(F.data.startswith("as_mark_scam:"))
async def cb_mark_scam(callback: CallbackQuery):
    message_db_id = _parse_message_id_from_callback(callback.data or "")
    if message_db_id is None:
        await callback.answer("Некорректный формат callback_data.", show_alert=True)
        return

    admin_chat_id = callback.message.chat.id
    global_admin_ids = parse_global_admin_chat_ids()

    with get_session() as session:
        msg_obj: db.Message | None = session.get(db.Message, message_db_id)
        if msg_obj is None:
            await callback.answer("Запись уже не найдена в базе.", show_alert=True)
            return

        chat_obj: db.Chat | None = (
            session.execute(select(db.Chat).where(db.Chat.id == msg_obj.chat_id))
            .scalars()
            .first()
        )
        if chat_obj is None:
            await callback.answer("Связанный чат не найден в базе.", show_alert=True)
            return

        is_global_admin_chat = admin_chat_id in global_admin_ids
        if not is_global_admin_chat and chat_obj.admin_chat_telegram_id != admin_chat_id:
            await callback.answer("Нет доступа к этой записи.", show_alert=True)
            return

        admin_user = _get_or_create_user_by_telegram(session, callback.from_user)
        msg_obj.human_label = "SCAM"
        msg_obj.human_labeled_at = dt.datetime.now(dt.timezone.utc)
        msg_obj.human_labeled_by = admin_user.id

        session.commit()

    try:
        old_text = callback.message.text or ""
        marker = "\n\n👮 Решение админа:"
        base_text = old_text.split(marker, 1)[0] if marker in old_text else old_text
        new_text = base_text + "\n\n👮 Решение админа: <b>СКАМ (подтверждено)</b>"
        await callback.message.edit_text(new_text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Failed to edit admin card text: %s", exc)

    await callback.answer("Скам подтверждён.")


def _parse_user_id_from_callback(data: str) -> int | None:
    try:
        _, id_str = data.split(":", 1)
        return int(id_str)
    except Exception:
        return None


def _is_forward_like_message(message: Message) -> bool:
    """Признаки, что сообщение является forwarded/пересылкой."""
    origin = getattr(message, "forward_origin", None)
    forward_from = getattr(message, "forward_from", None)
    forward_date = getattr(message, "forward_date", None)
    forward_sender_name = getattr(message, "forward_sender_name", None)
    forward_from_chat = getattr(message, "forward_from_chat", None)

    return any(
        [
            origin is not None,
            forward_from is not None,
            forward_from_chat is not None,
            forward_sender_name is not None,
            forward_date is not None,
        ]
    )


def _admin_forward_filter(message: Message) -> bool:
    """
    КРИТИЧНО: этот фильтр нужен, чтобы handler не перекрывал весь поток сообщений.
    Матчим ТОЛЬКО forwarded-похожие сообщения и ТОЛЬКО в админ-чатах.
    """
    if _is_command_message(message):
        return False
    if not _is_forward_like_message(message):
        return False
    try:
        with get_session() as session:
            return _is_admin_chat(session, message.chat.id)
    except Exception as exc:
        logger.error("Failed to check admin chat in forward filter: %s", exc, exc_info=True)
        return False


@router.message(_admin_forward_filter)
async def admin_forwarded_message_panel(message: Message, bot: Bot):
    """Панель на forwarded. Если форвард скрытый — автора не получить."""
    origin = getattr(message, "forward_origin", None)
    forward_from = getattr(message, "forward_from", None)

    tg_user = None
    if origin is not None and getattr(origin, "sender_user", None) is not None:
        tg_user = origin.sender_user
    elif forward_from is not None:
        tg_user = forward_from

    if tg_user is None:
        await message.reply(
            "Не могу определить автора пересланного сообщения (скрытая пересылка или источник без user_id)."
        )
        return

    user_display = tg_user.full_name or (tg_user.username or f"id {tg_user.id}")
    text = (
        "🛠 <b>Действия по пользователю</b>\n"
        f"🙍‍♂️ <b>{html.escape(user_display)}</b> (<code>{tg_user.id}</code>)\n\n"
        "Выберите действие ниже."
    )

    await message.reply(
        text,
        parse_mode="HTML",
        reply_markup=_build_user_action_keyboard(tg_user.id),
    )


async def _edit_or_reply_panel_result(
    callback: CallbackQuery,
    *,
    title: str,
    body_lines: list[str],
    tg_user_id: int,
):
    text = "\n".join([f"🛠 <b>{html.escape(title)}</b>", *body_lines])
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=_build_user_action_keyboard(tg_user_id),
        )
    except Exception as exc:
        logger.warning("Failed to edit admin panel message: %s", exc)
        try:
            await callback.message.reply(text, parse_mode="HTML")
        except Exception:
            pass


@router.callback_query(F.data.startswith("as_u_wl:"))
async def cb_user_whitelist(callback: CallbackQuery):
    tg_user_id = _parse_user_id_from_callback(callback.data or "")
    if tg_user_id is None:
        await callback.answer("Некорректный формат callback_data.", show_alert=True)
        return

    changed = _set_global_whitelist(tg_user_id)

    status = (
        "✅ Добавлен в <b>global whitelist</b>."
        if changed
        else "ℹ️ Уже был в global whitelist."
    )

    try:
        old_text = callback.message.text or ""
        marker = "\n\n👮 Действие админа:"
        base_text = old_text.split(marker, 1)[0] if marker in old_text else old_text
        new_text = (
            base_text
            + f"\n\n👮 Действие админа: <b>Whitelist</b>"
            + f"\n🙍‍♂️ <code>{tg_user_id}</code> — {status}"
        )
        await callback.message.edit_text(new_text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Failed to edit admin card text: %s", exc)

    await callback.answer("Готово.")


@router.callback_query(F.data.startswith("as_u_rs:"))
async def cb_user_reset_strikes(callback: CallbackQuery):
    tg_user_id = _parse_user_id_from_callback(callback.data or "")
    if tg_user_id is None:
        await callback.answer("Некорректный формат callback_data.", show_alert=True)
        return

    updated = _reset_strikes_in_db(tg_user_id)

    lines = [
        f"🙍‍♂️ Пользователь: <code>{tg_user_id}</code>",
        f"✅ Обнулено страйков (membership найдено): <b>{updated}</b>",
    ]
    await _edit_or_reply_panel_result(
        callback,
        title="Обнуление страйков",
        body_lines=lines,
        tg_user_id=tg_user_id,
    )
    await callback.answer("Готово.")


@router.callback_query(F.data.startswith("as_u_unban:"))
async def cb_user_unban(callback: CallbackQuery, bot: Bot):
    tg_user_id = _parse_user_id_from_callback(callback.data or "")
    if tg_user_id is None:
        await callback.answer("Некорректный формат callback_data.", show_alert=True)
        return

    ok, fail, errors = await _mass_unban_and_unmute(bot, tg_user_id)

    lines = [
        f"🙍‍♂️ Пользователь: <code>{tg_user_id}</code>",
        f"✅ Успешно: <b>{ok}</b>",
        f"⚠️ Ошибок: <b>{fail}</b>",
    ]
    if errors:
        lines.append("")
        lines.append("Первые ошибки:")
        lines.extend(errors)

    await _edit_or_reply_panel_result(
        callback,
        title="Разбан / размут",
        body_lines=lines,
        tg_user_id=tg_user_id,
    )
    await callback.answer("Готово.")


@router.callback_query(F.data.startswith("as_u_ban:"))
async def cb_user_ban(callback: CallbackQuery, bot: Bot):
    tg_user_id = _parse_user_id_from_callback(callback.data or "")
    if tg_user_id is None:
        await callback.answer("Некорректный формат callback_data.", show_alert=True)
        return

    ok, fail, errors = await _mass_ban(bot, tg_user_id)

    result_parts = [
        f"Успешно: <b>{ok}</b>",
        f"Ошибок: <b>{fail}</b>",
    ]
    if errors:
        result_parts.extend(errors[:5])

    try:
        old_text = callback.message.text or ""
        marker = "\n\n👮 Действие админа:"
        base_text = old_text.split(marker, 1)[0] if marker in old_text else old_text
        new_text = (
            base_text
            + f"\n\n👮 Действие админа: <b>Бан</b>"
            + f"\n🙍‍♂️ <code>{tg_user_id}</code>"
            + "\n" + "\n".join(result_parts)
        )
        await callback.message.edit_text(new_text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Failed to edit admin card text: %s", exc)

    await callback.answer("Готово.")


# ---------------------------------------------------------------------------
# Управление деактивированными чатами
# ---------------------------------------------------------------------------

@router.message(Command("as_deactivated"))
async def cmd_as_deactivated(message: Message):
    """Показать список деактивированных чатов."""
    if not _require_admin_chat(message):
        await message.reply("Команда доступна только в админ-чате.")
        return

    with get_session() as session:
        chats = (
            session.execute(
                select(db.Chat).where(
                    db.Chat.is_active == False,  # noqa: E712
                )
            )
            .scalars()
            .all()
        )

        if not chats:
            await message.reply("✅ Нет деактивированных чатов.", parse_mode="HTML")
            return

        snapshot = [
            {
                "telegram_chat_id": c.telegram_chat_id,
                "title": c.title,
                "reason": c.deactivation_reason,
                "deactivated_at": c.deactivated_at,
            }
            for c in chats
        ]

    lines: list[str] = [f"🔇 <b>Деактивированные чаты ({len(snapshot)})</b>\n"]
    for c in snapshot[:20]:
        title = html.escape(c["title"] or "(без названия)")
        reason = html.escape(c["reason"] or "—")
        ts = "—"
        if c["deactivated_at"]:
            try:
                ts = c["deactivated_at"].strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts = str(c["deactivated_at"])
        lines.append(
            f"• <b>{title}</b> (<code>{c['telegram_chat_id']}</code>)\n"
            f"  Причина: <code>{reason}</code> | {ts}"
        )
    if len(snapshot) > 20:
        lines.append(f"\n… и ещё {len(snapshot) - 20} чатов.")

    lines.append(
        "\n\nДля реактивации: <code>/as_reactivate CHAT_ID</code>"
    )
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("as_reactivate"))
async def cmd_as_reactivate(message: Message):
    """Реактивировать деактивированный чат."""
    if not _require_admin_chat(message):
        await message.reply("Команда доступна только в админ-чате.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(
            "Использование: <code>/as_reactivate CHAT_ID</code>",
            parse_mode="HTML",
        )
        return

    try:
        tg_chat_id = int(parts[1].strip())
    except ValueError:
        await message.reply("ID чата должен быть числом.")
        return

    changed = reactivate_chat(tg_chat_id)
    if changed:
        await message.reply(
            f"✅ Чат <code>{tg_chat_id}</code> реактивирован и снова участвует в массовых операциях.",
            parse_mode="HTML",
        )
    else:
        await message.reply(
            f"ℹ️ Чат <code>{tg_chat_id}</code> не найден среди деактивированных "
            "(возможно, уже активен или не существует в БД).",
            parse_mode="HTML",
        )