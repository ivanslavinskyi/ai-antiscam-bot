import logging
import html
import datetime as dt
from typing import List

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select, func, or_

from app.config import settings
from app.db.session import get_session
from app.db import models as db

logger = logging.getLogger(__name__)

router = Router(name="admin")

@router.message(Command("help", "as_help"))
async def cmd_as_help(message: Message):
    """
    Справка по командам анти-скам бота.
    """
    text = (
        "ℹ️ <b>Справка по анти-скам боту</b>\n\n"
        "<b>Что делает бот:</b>\n"
        "• Проверяет каждое сообщение через LLM (OpenAI).\n"
        "• Автоматически удаляет скам и выдаёт страйки нарушителям.\n"
        "• Отправляет карточки скам-сообщений в админ-чаты с кнопками для разметки.\n\n"

        "<b>Команды в РАБОЧИХ чатах (где сидят участники):</b>\n"
        "• <code>/as_set_admin_chat &lt;id_админ-чата&gt;</code> — привязать этот рабочий чат к админ-чату.\n"
        "  После этого все уведомления о скаме и аналитика по этому чату будут доступны в указанном админ-чате.\n"
        "• <code>/as_status</code> — показать статус для этого рабочего чата:\n"
        "  к какому админ-чату привязан и краткую статистику.\n\n"

        "<b>Команды в АДМИН-чатах:</b>\n"
        "• <code>/as_status</code> — показать, какие рабочие чаты привязаны к этому админ-чату,\n"
        "  и является ли этот чат глобальным админ-чатом.\n"
        "• <code>/as_stats</code> — сводная статистика по всем рабочим чатам,\n"
        "  которые привязаны к этому админ-чату:\n"
        "  – сколько сообщений проверено;\n"
        "  – сколько скамов по модели;\n"
        "  – сколько скамов подтверждено админами;\n"
        "  – сколько помечено как НЕ скам;\n"
        "  – топ-нарушители.\n\n"
        "• <code>/as_recent</code> или <code>/as_recent N</code> — последние N скам-сообщений\n"
        "  по рабочим чатам, привязанным к этому админ-чату (по умолчанию 10, максимум 50).\n\n"

        "<b>Кнопки под уведомлениями о скаме в админ-чатах:</b>\n"
        "• <b>✅ Не скам</b> — помечает сообщение как НЕ скам, сохраняет решение админа в базе.\n"
        "• <b>🚫 Точно скам</b> — подтверждает, что сообщение — скам, также сохраняет решение.\n"
        "  Эти решения используются как разметка для будущей обучающей выборки.\n\n"
    )

    await message.answer(text, parse_mode="HTML")


@router.message(Command("as_status"))
async def cmd_as_status(message: Message, bot: Bot):
    """
    Показать статус для текущего чата:
    - если это рабочий чат: к какому админ-чату он привязан и локальную статистику
      (в рабочих чатах доступно только администраторам);
    - если это админ-чат: какие рабочие чаты к нему привязаны.
    """
    chat_id = message.chat.id
    chat_type = message.chat.type
    global_admin_ids = parse_global_admin_chat_ids()

    with get_session() as session:
        # Чат как РАБОЧИЙ (по telegram_chat_id)
        working_chat: db.Chat | None = (
            session.execute(
                select(db.Chat).where(db.Chat.telegram_chat_id == chat_id)
            )
            .scalars()
            .first()
        )

        # Чаты, для которых ЭТОТ чат является админ-чатом
        managed_chats = _get_managed_chats(session, chat_id)

        # Статистика только для одного рабочего чата, если он есть
        msgs_total = msgs_scam_model = msgs_scam_human = msgs_not_scam_human = 0
        if working_chat is not None:
            msgs_total = (
                session.execute(
                    select(func.count(db.Message.id)).where(
                        db.Message.chat_id == working_chat.id
                    )
                ).scalar_one()
                or 0
            )
            msgs_scam_model = (
                session.execute(
                    select(func.count(db.Message.id)).where(
                        db.Message.chat_id == working_chat.id,
                        db.Message.model_label == "SCAM",
                    )
                ).scalar_one()
                or 0
            )
            msgs_scam_human = (
                session.execute(
                    select(func.count(db.Message.id)).where(
                        db.Message.chat_id == working_chat.id,
                        db.Message.human_label == "SCAM",
                    )
                ).scalar_one()
                or 0
            )
            msgs_not_scam_human = (
                session.execute(
                    select(func.count(db.Message.id)).where(
                        db.Message.chat_id == working_chat.id,
                        db.Message.human_label == "NOT_SCAM",
                    )
                ).scalar_one()
                or 0
            )

    # 🔹 Сценарий 1: РАБОЧИЙ чат — тут включаем проверку на админа
    if chat_type in ("group", "supergroup") and working_chat is not None:
        # Разрешаем только администраторам рабочего чата
        if message.from_user is None:
            await message.answer("Не удалось определить отправителя команды.")
            return

        member = await bot.get_chat_member(chat_id, message.from_user.id)
        if member.status not in ("administrator", "creator"):
            await message.answer(
                "В рабочих чатах команды анти-скам бота доступны только администраторам."
            )
            return

        admin_chat_id = working_chat.admin_chat_telegram_id
        admin_part = (
            f"<code>{admin_chat_id}</code>"
            if admin_chat_id is not None
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
        lines.append(
            "Изменить админ-чат можно командой:\n"
            f"<code>/as_set_admin_chat &lt;id_админ-чата&gt;</code>"
        )

        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    # 🔹 Сценарий 2: АДМИН-чат (управляет другими чатами) — тут можно не ограничивать
    if managed_chats:
        lines = []
        lines.append("ℹ️ <b>Статус админ-чата</b>")
        lines.append(f"Чат: <b>{html.escape(message.chat.title or '(без названия)')}</b>")
        lines.append(f"ID: <code>{chat_id}</code>")
        lines.append("")
        is_global = chat_id in global_admin_ids
        if is_global:
            lines.append("Роль: <b>глобальный админ-чат</b> (видит все чаты из .env).")
        else:
            lines.append("Роль: <b>локальный админ-чат</b>.")
        lines.append("")
        lines.append(
            f"К этому админ-чату привязано рабочих чатов: <b>{len(managed_chats)}</b>"
        )

        # Список до 10 чатов
        for c in managed_chats[:10]:
            title = html.escape(c.title or "(без названия)")
            lines.append(f"• <b>{title}</b> (<code>{c.telegram_chat_id}</code>)")

        if len(managed_chats) > 10:
            lines.append(f"… и ещё {len(managed_chats) - 10} чатов.")

        lines.append("")
        lines.append(
            "Команды для аналитики:\n"
            "• <code>/as_stats</code> — сводная статистика.\n"
            "• <code>/as_recent</code> — последние скам-сообщения."
        )

        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    # 🔹 Сценарий 3: чат пока никуда не привязан и никем не управляет
    is_global = chat_id in global_admin_ids
    lines = []
    lines.append("ℹ️ <b>Статус чата</b>")
    lines.append(f"ID: <code>{chat_id}</code>")
    lines.append("")
    if is_global:
        lines.append(
            "Этот чат указан в переменной <code>ADMIN_CHAT_IDS</code> как глобальный админ-чат, "
            "но к нему пока не привязано ни одного рабочего чата."
        )
    else:
        lines.append(
            "Этот чат не привязан как рабочий и не используется как админ-чат."
        )
        lines.append(
            "Чтобы привязать рабочий чат к этому, вызови в рабочем чате:\n"
            f"<code>/as_set_admin_chat {chat_id}</code>"
        )

    await message.answer("\n".join(lines), parse_mode="HTML")



def parse_global_admin_chat_ids() -> List[int]:
    """
    Разбираем admin_chat_ids из .env в список int.
    Используется для глобальных (супер) админ-чатов.
    """
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


def _get_managed_chats(session, admin_chat_id: int) -> List[db.Chat]:
    """
    Возвращает список рабочих чатов, для которых данный чат является админ-чатом.
    """
    return (
        session.execute(
            select(db.Chat).where(db.Chat.admin_chat_telegram_id == admin_chat_id)
        )
        .scalars()
        .all()
    )


@router.message(Command("as_set_admin_chat"))
async def cmd_as_set_admin_chat(message: Message, bot: Bot):
    """
    Привязка рабочего чата к админ-чату.
    Команду нужно выполнить В РАБОЧЕМ чате, передав ID админ-чата:
      /as_set_admin_chat ID

    Доступно только администраторам этого рабочего чата.
    """
    # 1. Команда должна выполняться только в группе/супергруппе
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Эту команду нужно выполнять в групповом чате, не в личке.")
        return

    # 2. Проверяем, что отправитель — админ или создатель
    if message.from_user is None:
        await message.answer("Не удалось определить отправителя команды.")
        return

    member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if member.status not in ("administrator", "creator"):
        await message.answer(
            "Только администраторы этого чата могут менять привязку к админ-чату."
        )
        return

    # 3. Парсим аргумент — ID админ-чата
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Укажи ID админ-чата, например:\n"
            "/as_set_admin_chat -1001234567890"
        )
        return

    try:
        admin_chat_id = int(parts[1].strip())
    except ValueError:
        await message.answer("ID админ-чата должен быть числом (обычно в формате -100...).")
        return

    # 4. Сохраняем привязку в БД
    with get_session() as session:
        chat = (
            session.execute(
                select(db.Chat).where(db.Chat.telegram_chat_id == message.chat.id)
            )
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
        "Для этого чата теперь используется админ-чат:\n"
        f"<code>{admin_chat_id}</code>\n\n"
        "Уведомления о скаме и аналитика по этому чату будут доступны там.",
        parse_mode="HTML",
    )


@router.message(Command("as_stats"))
async def cmd_as_stats(message: Message):
    """
    Краткая статистика по всем рабочим чатам, привязанным к этому админ-чату.
    """
    admin_chat_id = message.chat.id

    with get_session() as session:
        chats = _get_managed_chats(session, admin_chat_id)
        if not chats:
            await message.answer(
                "Этот чат пока не привязан ни к одному рабочему чату.\n\n"
                "Выполни в рабочем чате:\n"
                f"<code>/as_set_admin_chat {admin_chat_id}</code>",
                parse_mode="HTML",
            )
            return

        # Сохраняем примитивные данные о чатах, чтобы не трогать ORM-объекты после выхода из сессии
        chat_infos = [
            {
                "id": c.id,
                "title": c.title,
                "telegram_chat_id": c.telegram_chat_id,
            }
            for c in chats
        ]
        chat_ids = [info["id"] for info in chat_infos]

        total_messages = (
            session.execute(
                select(func.count(db.Message.id)).where(
                    db.Message.chat_id.in_(chat_ids)
                )
            ).scalar_one()
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

        total_human_labeled = (
            session.execute(
                select(func.count(db.Message.id)).where(
                    db.Message.chat_id.in_(chat_ids),
                    db.Message.human_label.is_not(None),
                )
            ).scalar_one()
            or 0
        )

        # Топ-5 пользователей по количеству скам-сообщений (по модели или по человеку)
        top_users_stmt = (
            select(
                db.User.username,
                db.User.first_name,
                func.count(db.Message.id).label("cnt"),
            )
            .join(db.Message, db.Message.user_id == db.User.id)
            .where(
                db.Message.chat_id.in_(chat_ids),
                or_(
                    db.Message.model_label == "SCAM",
                    db.Message.human_label == "SCAM",
                ),
            )
            .group_by(db.User.id)
            .order_by(func.count(db.Message.id).desc())
            .limit(5)
        )
        top_rows = session.execute(top_users_stmt).all()

        # Сохраняем топ-юзеров как обычные dict'ы
        top_users = [
            {
                "username": username,
                "first_name": first_name,
                "cnt": cnt,
            }
            for (username, first_name, cnt) in top_rows
        ]

    # Здесь сессии уже нет — работаем только с примитивами (dict, int, str)
    lines: list[str] = []
    lines.append("📊 <b>Статистика анти-скам бота</b>")
    if len(chat_infos) == 1:
        title = chat_infos[0]["title"] or "(без названия)"
        lines.append(f"По чату: <b>{html.escape(title)}</b>")
    else:
        lines.append(
            f"По {len(chat_infos)} рабочим чатам, привязанным к этому админ-чату."
        )
    lines.append("")
    lines.append(f"Всего проверенных сообщений: <b>{total_messages}</b>")
    lines.append(f"Скам по модели: <b>{total_scam_model}</b>")
    lines.append(f"Скам по решению админов: <b>{total_scam_human}</b>")
    lines.append(f"Помечено как НЕ скам: <b>{total_not_scam_human}</b>")
    lines.append(f"Сообщений с ручной разметкой: <b>{total_human_labeled}</b>")

    if top_users:
        lines.append("")
        lines.append("👥 Топ-5 подозрительных пользователей:")
        for i, user in enumerate(top_users, start=1):
            name_part = user["username"] or user["first_name"] or "(без имени)"
            name_part = html.escape(name_part)
            cnt = user["cnt"]
            lines.append(f"{i}. {name_part} — <b>{cnt}</b> скам-сообщений")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("as_recent"))
async def cmd_as_recent(message: Message):
    """
    Показать последние N (по умолчанию 10) скам-сообщений для рабочих чатов,
    привязанных к этому админ-чату.
    """
    admin_chat_id = message.chat.id

    # Можно передать параметр: /as_recent 20
    parts = (message.text or "").split(maxsplit=1)
    limit = 10
    if len(parts) == 2:
        try:
            limit = max(1, min(50, int(parts[1].strip())))
        except ValueError:
            pass

    with get_session() as session:
        chats = _get_managed_chats(session, admin_chat_id)
        if not chats:
            await message.answer(
                "Этот чат пока не привязан ни к одному рабочему чату.\n\n"
                "Выполни в рабочем чате:\n"
                f"<code>/as_set_admin_chat {admin_chat_id}</code>",
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
                or_(
                    db.Message.model_label == "SCAM",
                    db.Message.human_label == "SCAM",
                ),
            )
            .order_by(db.Message.created_at.desc())
            .limit(limit)
        )
        db_rows = session.execute(stmt).all()

        # Сохраняем только нужные поля в обычных словарях
        rows: list[dict] = []
        for msg_obj, user_obj, chat_obj in db_rows:
            rows.append(
                {
                    "created_at": msg_obj.created_at,
                    "text": msg_obj.text,
                    "chat_title": chat_obj.title,
                    "user_username": user_obj.username,
                    "user_first_name": user_obj.first_name,
                    "user_telegram_user_id": user_obj.telegram_user_id,
                }
            )

    if not rows:
        await message.answer("Пока нет ни одного скам-сообщения в этих чатах.")
        return

    lines: list[str] = []
    lines.append(f"🕒 Последние {len(rows)} скам-сообщений:")
    for row in rows:
        created_at = row["created_at"]
        if created_at and getattr(created_at, "tzinfo", None):
            created_at = created_at.astimezone(dt.timezone.utc)
        ts = created_at.strftime("%Y-%m-%d %H:%M") if created_at else "—"

        chat_title = html.escape(row["chat_title"] or "(без названия)")

        user_name = (
            row["user_username"]
            or row["user_first_name"]
            or f"id {row['user_telegram_user_id']}"
        )
        user_name = html.escape(user_name)

        text = row["text"] or ""
        if len(text) > 120:
            text = text[:117] + "..."
        text = text.replace("\n", " ")
        text = html.escape(text)

        lines.append(
            f"• [{ts}] <b>{chat_title}</b> — {user_name}: <code>{text}</code>"
        )

    await message.answer("\n".join(lines), parse_mode="HTML")



def _parse_message_id_from_callback(data: str) -> int | None:
    try:
        _, id_str = data.split(":", 1)
        return int(id_str)
    except Exception:
        return None


def _get_or_create_user_by_telegram(session, tg_user) -> db.User:
    user = (
        session.execute(
            select(db.User).where(db.User.telegram_user_id == tg_user.id)
        )
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
        # немного обновим данные
        user.username = tg_user.username
        user.first_name = tg_user.first_name
        user.last_name = tg_user.last_name
    return user


@router.callback_query(F.data.startswith("as_not_scam:"))
async def cb_not_scam(callback: CallbackQuery):
    """
    Кнопка в админ-чате: пометить сообщение как НЕ скам.
    """
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
            session.execute(
                select(db.Chat).where(db.Chat.id == msg_obj.chat_id)
            )
            .scalars()
            .first()
        )
        if chat_obj is None:
            await callback.answer("Связанный чат не найден в базе.", show_alert=True)
            return

        is_global_admin_chat = admin_chat_id in global_admin_ids
        if not is_global_admin_chat and chat_obj.admin_chat_telegram_id != admin_chat_id:
            await callback.answer(
                "У этого админ-чата нет доступа к этой записи.", show_alert=True
            )
            return

        # Записываем ручную разметку
        admin_user = _get_or_create_user_by_telegram(session, callback.from_user)
        msg_obj.human_label = "NOT_SCAM"
        msg_obj.human_labeled_at = dt.datetime.now(dt.timezone.utc)
        msg_obj.human_labeled_by = admin_user.id

        session.commit()

    try:
        # Обновим текст карточки в админ-чате
        old_text = callback.message.text or ""
        marker = "\n\n👮 Решение админа:"
        if marker in old_text:
            base_text = old_text.split(marker, 1)[0]
        else:
            base_text = old_text
        new_text = (
            base_text
            + "\n\n👮 Решение админа: <b>НЕ СКАМ</b>"
        )
        await callback.message.edit_text(new_text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Failed to edit admin card text: %s", exc)

    await callback.answer("Помечено как НЕ скам.")


@router.callback_query(F.data.startswith("as_mark_scam:"))
async def cb_mark_scam(callback: CallbackQuery):
    """
    Кнопка в админ-чате: подтвердить, что сообщение — скам.
    """
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
            session.execute(
                select(db.Chat).where(db.Chat.id == msg_obj.chat_id)
            )
            .scalars()
            .first()
        )
        if chat_obj is None:
            await callback.answer("Связанный чат не найден в базе.", show_alert=True)
            return

        is_global_admin_chat = admin_chat_id in global_admin_ids
        if not is_global_admin_chat and chat_obj.admin_chat_telegram_id != admin_chat_id:
            await callback.answer(
                "У этого админ-чата нет доступа к этой записи.", show_alert=True
            )
            return

        # Записываем ручную разметку
        admin_user = _get_or_create_user_by_telegram(session, callback.from_user)
        msg_obj.human_label = "SCAM"
        msg_obj.human_labeled_at = dt.datetime.now(dt.timezone.utc)
        msg_obj.human_labeled_by = admin_user.id

        session.commit()

    try:
        # Обновим текст карточки в админ-чате
        old_text = callback.message.text or ""
        marker = "\n\n👮 Решение админа:"
        if marker in old_text:
            base_text = old_text.split(marker, 1)[0]
        else:
            base_text = old_text
        new_text = (
            base_text
            + "\n\n👮 Решение админа: <b>СКАМ (подтверждено)</b>"
        )
        await callback.message.edit_text(new_text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Failed to edit admin card text: %s", exc)

    await callback.answer("Скам подтверждён.")
