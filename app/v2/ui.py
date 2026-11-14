import json
import io
import uuid
from contextvars import ContextVar
from datetime import timedelta

from aiogram.exceptions import TelegramBadRequest

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BufferedInputFile,
    ReplyKeyboardRemove,
)
from sqlalchemy import select, func, case
from sqlalchemy.dialects.postgresql import insert

from app.v2 import tables as t
from app.v2.domain import Denied, Conflict, Policy, GroupScope, Role, now
from app.v2.operator import parse_import
from app.v2 import controls
from app.v2 import finance
from app.v2.auth import assert_scope
from app.v2.state import ensure_user, ensure_member
from app.v2.presentation import (
    label as display_label,
    person,
    setting_value,
    button_text,
    telegram_member_status,
)
from app.v2.card_view import incident_text, code_entities
from app.v2.design_controls import DesignControls
from app.v2.team_ui import remember_contact
from app.v2.onboarding_people import PeopleControls


def clip(value, limit=3600):
    raw = str(value).encode("utf-16-le")
    return raw[: limit * 2].decode("utf-16-le", errors="ignore") + (
        "…" if len(raw) > limit * 2 else ""
    )


def keyboard(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=button_text(label, data), callback_data=data)
                for label, data in row
            ]
            for row in rows
        ]
    )


LABELS = {
    "history": "История",
    "review": "На рассмотрении",
    "trust": "Вайтлист",
    "deny": "Блэклист",
    "audit": "Журнал",
    "actions": "Действия Telegram",
    "jobs": "Очередь",
    "roles": "Роли",
}
SETTINGS = {
    "auto_whitelist_enabled": "Автовайтлист",
    "auto_whitelist_threshold": "Сообщений до доверия",
    "confidence_threshold": "Порог уверенности",
    "warn_at": "Предупреждение: страйков",
    "mute_at": "Мут: страйков",
    "ban_at": "Бан: страйков",
    "mute_seconds": "Длительность мута",
    "mute_enabled": "Автоматический мут",
    "ban_enabled": "Автоматический бан",
    "exempt_admins": "Не проверять администраторов",
    "notice_ttl_seconds": "Удалять предупреждения через",
    "review_enabled": "Карточки срабатываний",
    "delete_when_admin_unavailable": "Удалять при недоступной админке",
    "timezone": "Часовой пояс",
    "daily_summary_time": "Время сводки",
    "quiet_start": "Тихие часы",
    "quiet_end": "Тихие часы: конец",
    "daily_budget_usd": "Дневной бюджет, USD",
    "context": "Описание сообщества",
    "max_action_age_seconds": "Не наказывать автоматически сообщения старше",
    "categories": "Категории для автоматических действий",
}

SETTING_SECTIONS = {
    "trust": (
        "Доверие",
        "Кого проверять и когда выдавать автоматический вайтлист.",
        ("auto_whitelist_enabled", "auto_whitelist_threshold", "exempt_admins"),
    ),
    "sanctions": (
        "Наказания",
        "Страйк — учтённый случай скама. Автонаказания действуют в режиме автоматической модерации.",
        ("warn_at", "mute_enabled", "mute_at", "mute_seconds", "ban_enabled", "ban_at"),
    ),
    "notifications": (
        "Уведомления",
        "Карточки приходят сразу. Тихие часы относятся только к дополнительным уведомлениям; проверки продолжаются.",
        (
            "review_enabled",
            "notice_ttl_seconds",
            "daily_summary_time",
            "quiet_start",
            "timezone",
        ),
    ),
    "advanced": (
        "Дополнительно",
        "Точные правила проверки и защиты от запоздалых действий. Обычно достаточно текущих значений.",
        (
            "confidence_threshold",
            "context",
            "categories",
            "max_action_age_seconds",
            "delete_when_admin_unavailable",
        ),
    ),
}


def policy_value(field, policy):
    value = policy[field]
    if field in {"mute_seconds", "notice_ttl_seconds", "max_action_age_seconds"}:
        if not value:
            return "Не удалять"
        for unit, suffix in ((86400, "дн."), (3600, "ч"), (60, "мин")):
            if value % unit == 0:
                return f"{value // unit} {suffix}"
        return f"{value} сек."
    if field == "quiet_start":
        return f"{value}–{policy['quiet_end']}" if value else "Выключены"
    if field == "categories":
        names = {
            "job_scam": "Работа",
            "crypto": "Крипто",
            "investment": "Инвестиции",
            "phishing": "Фишинг",
            "other": "Другое",
        }
        return ", ".join(names[v] for v in value) or "Нет"
    if field == "context" and not value:
        return "Не задано"
    return setting_value(value)


REASONS = {
    "trusted": "Проверенный участник",
    "scam": "Подтверждённый скам",
    "rules": "Нарушение правил группы",
    "review": "Доверие требует пересмотра",
    "resolved": "Основание для ограничения снято",
    "reset": "Пересмотр страйков модератором",
}
ACTION_REASONS = {
    "trust_add": ("trusted",),
    "trust_remove": ("review", "rules"),
    "deny_add": ("scam", "rules"),
    "deny_remove": ("resolved",),
    "ban": ("scam", "rules"),
    "unban": ("resolved",),
    "mute": ("rules", "scam"),
    "unmute": ("resolved",),
    "reset_strikes": ("reset",),
}


def setting_prompt(key):
    default = Policy().model_dump()[key]
    hints = {
        "mute_seconds": "Введите длительность в секундах: 3600 — 1 час, 86400 — 1 день, 604800 — 7 дней.",
        "notice_ttl_seconds": "Через сколько секунд удалять короткие предупреждения бота в рабочей группе. 60 — минута, 0 — не удалять. Карточки в админке сохраняются.",
        "max_action_age_seconds": "Максимальный возраст сообщения для автоматического наказания, в секундах. 900 — 15 минут. Более старые случаи остаются для ручного решения.",
        "auto_whitelist_threshold": "Введите целое число от 1 до 1000, например 5. Это число подходящих публикаций до доверия только в этой группе. Уже выданное доверие сохранится.",
        "confidence_threshold": "Введите число от 0 до 1, например 0.8.",
        "timezone": "Введите часовой пояс, например Europe/Zurich.",
        "daily_summary_time": "Введите время сводки, например 20:00, или «нет», чтобы отключить.",
        "quiet_start": "Введите начало и конец тихих часов через пробел, например 22:00 08:00. Для отключения — «нет».",
        "quiet_end": "Введите начало и конец тихих часов через пробел, например 22:00 08:00. Для отключения — «нет».",
        "daily_budget_usd": "Введите дневной бюджет этой группы в USD, например 0.5. Чтобы убрать лимит группы — «нет»; общий лимит продолжит действовать.",
        "context": "Опишите тематику и правила сообщества, до 2000 символов.",
        "categories": "Перечислите через запятую: job_scam (работа), crypto (крипто), investment (инвестиции), phishing (фишинг), other (другое).",
    }
    hint = hints.get(key)
    if hint is None:
        hint = (
            "Введите «вкл» или «выкл»."
            if isinstance(default, bool)
            else "Введите целое число."
        )
    return f"{SETTINGS[key]}\n{hint}\nИзменение применяется после подтверждения. Для отмены нажмите «В меню»."


def incident_buttons(gid, row, readonly=False):
    iid = row["id"].hex
    base = f"v:q:{gid}:{iid}:{row['version']:x}"
    rows = []

    def restriction(kind, remove, caption, add_code, remove_code):
        actions = [
            a
            for a in row.get("actions", [])
            if a["kind"] in {kind, remove} and a["state"] not in {"failed", "cancelled"}
        ]
        last = actions[-1] if actions else None
        if last and last["state"] in {"planned", "in_flight", "unknown"}:
            return ("Проверить " + caption.lower(), f"v:details:{gid}:{iid}")
        if last and last["state"] == "succeeded" and last["kind"] == kind:
            return (
                "Разбан" if kind == "ban" else "Снять мут",
                base + ":" + remove_code,
            )
        return (caption, base + ":" + add_code)

    if not readonly:
        if row.get("human_label"):
            rows.append([("Изменить решение", f"v:revise:{gid}:{iid}")])
        else:
            rows.append([("Не скам", base + ":n"), ("Скам", base + ":s")])
        if row["user_id"]:
            trust = (
                ("Убрать вайтлист", f"v:tgremove:{gid}:{row['trust_grant'].hex}")
                if row.get("trust_grant")
                else ("Вайтлист", base + ":w")
            )
            rows.append([restriction("ban", "unban", "Бан", "b", "x"), trust])
        if not row["legacy"]:
            deleted = any(
                a["kind"] == "delete"
                and a["state"] in {"planned", "in_flight", "succeeded", "unknown"}
                for a in row.get("actions", [])
            )
            buttons = [] if deleted else [("Удалить", base + ":d")]
            if row["user_id"]:
                buttons.append(restriction("mute", "unmute", "Мут на 24 ч", "m", "u"))
            if buttons:
                rows.append(buttons)
    rows.append([("Подробнее", f"v:details:{gid}:{iid}"), ("Меню", f"v:menu:{gid}")])
    return keyboard(rows)


_panel_message = ContextVar("admin_panel_message", default=None)


class UI(PeopleControls, DesignControls):
    def __init__(self, engine, bot, auth, operator, onboarding):
        self.engine, self.bot, self.auth = engine, bot, auth
        self.operator, self.onboarding = operator, onboarding

    async def send(self, chat, text, buttons=None, entities=None):
        return await self.bot.send_message(
            chat, clip(text), reply_markup=buttons, parse_mode=None, entities=entities
        )

    async def panel(self, chat, text, buttons=None, message_id=None):
        target = (chat, message_id) if message_id else _panel_message.get()
        if target and target[0] == chat:
            try:
                return await self.bot.edit_message_text(
                    clip(text),
                    chat_id=chat,
                    message_id=target[1],
                    reply_markup=buttons,
                    parse_mode=None,
                )
            except TelegramBadRequest as exc:
                if "message is not modified" in exc.message.lower():
                    return
        return await self.send(chat, text, buttons)

    async def session(self, actor, admin):
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(t.sessions).where(
                            t.sessions.c.actor_id == actor,
                            t.sessions.c.admin_chat_id == admin,
                            t.sessions.c.expires_at > now(),
                        )
                    )
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    async def save_session(self, actor, admin, gid=None, **state):
        async with self.engine.begin() as conn:
            stmt = insert(t.sessions).values(
                actor_id=actor,
                admin_chat_id=admin,
                chat_id=gid,
                state=state,
                expires_at=now() + timedelta(minutes=30),
            )
            await conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=["actor_id", "admin_chat_id"],
                    set_={
                        "chat_id": gid,
                        "state": state,
                        "expires_at": stmt.excluded.expires_at,
                    },
                )
            )

    async def menu(self, scope):
        await self.admin_home(scope.actor_id, scope.admin_chat_id)
        await self.save_session(scope.actor_id, scope.admin_chat_id, scope.chat_id)

    async def search_menu(self, scope, notice=""):
        scope.require("read")
        await self.save_session(
            scope.actor_id, scope.admin_chat_id, scope.chat_id, form="search"
        )
        await self.send(
            scope.admin_chat_id,
            (notice + "\n\n" if notice else "") + "Как найти участника\n"
            "• Нажмите «Участники» и выберите профиль кнопкой.\n"
            "• Напишите имя, часть имени или @username.\n"
            "• Перешлите его сообщение, если Telegram показывает автора.\n"
            "• Откройте профиль прямо из карточки сообщения.\n\n"
            "Поиск идёт только по известным боту участникам этой рабочей группы. "
            "При совпадении имён выберите нужный профиль. Числовой ID тоже поддерживается.",
            keyboard(
                [
                    [("Участники", f"v:members:{scope.chat_id}:0")],
                    [("История сообщений", f"v:page:{scope.chat_id}:history")],
                    [("В меню", f"v:menu:{scope.chat_id}")],
                ]
            ),
        )

    async def participants(self, scope, query="", offset=0):
        rows, more = await self.operator.members(scope, query, offset)
        await self.save_session(
            scope.actor_id,
            scope.admin_chat_id,
            scope.chat_id,
            form="search",
            member_query=query,
        )
        text = (
            f"Результаты поиска: {query}\n"
            if query
            else "Участники этой рабочей группы\n"
        )
        text += (
            "Выберите профиль кнопкой. Можно написать имя или @username для поиска.\n"
        )
        if not rows:
            text += "Совпадений нет. Откройте автора из истории сообщений или перешлите сообщение с открытым автором."
        else:
            text += "Это известные боту профили, включая исторические; текущее членство проверяется в профиле."
        buttons = [
            [
                (
                    clip(person(row), 45) + f" · {row['telegram_user_id']}",
                    f"v:user:{scope.chat_id}:{row['id']}",
                )
            ]
            for row in rows
        ]
        nav = []
        if offset:
            nav.append(
                ("Назад", f"v:memberspage:{scope.chat_id}:{max(0, offset - 12)}")
            )
        if more:
            nav.append(("Дальше", f"v:memberspage:{scope.chat_id}:{offset + 12}"))
        if nav:
            buttons.append(nav)
        buttons.extend(
            [
                [("Новый поиск", f"v:form:{scope.chat_id}:search")],
                [("В меню", f"v:menu:{scope.chat_id}")],
            ]
        )
        await self.send(scope.admin_chat_id, text, keyboard(buttons))

    async def confirmation(self, scope, kind, payload, version=None, description=""):
        token = await self.operator.issue(scope, kind, payload, version)
        async with self.engine.connect() as conn:
            title = (
                await conn.execute(
                    select(t.chats.c.title).where(t.chats.c.id == scope.chat_id)
                )
            ).scalar_one()
        target = ""
        if payload.get("user_id"):
            participant = await self.operator.profile(scope, payload["user_id"])
            target = f"👤 {person(participant)} ({participant['telegram_user_id']})\n"
        await self.send(
            scope.admin_chat_id,
            f"👥 {title}\n{target}{description}\n"
            "Подтверждение действует 5 минут и доступно только вам.",
            keyboard(
                [
                    [("Подтвердить", "v:confirm:" + token)],
                    [("Отмена", "v:cancel:" + token)],
                ]
            ),
        )

    async def show_card(self, scope, iid):
        row = await self.operator.detail(scope, iid)
        sent = await self.send(
            scope.admin_chat_id,
            incident_text(row),
            incident_buttons(
                scope.chat_id,
                row,
                scope.binding_version == 0 or scope.role == Role.OBSERVER,
            ),
            entities=code_entities(incident_text(row), row.get("telegram_user_id")),
        )
        if scope.binding_version:
            async with self.engine.begin() as conn:
                await conn.execute(
                    t.cards.insert().values(
                        chat_id=scope.chat_id,
                        incident_id=row["id"],
                        admin_chat_id=scope.admin_chat_id,
                        message_id=sent.message_id,
                        binding_version=scope.binding_version,
                        version=row["version"],
                    )
                )

    async def profile(self, scope, uid):
        row = await self.operator.profile(scope, uid)
        await self.save_session(scope.actor_id, scope.admin_chat_id, scope.chat_id)
        gid = scope.chat_id
        async with self.engine.connect() as conn:
            tgchat = (
                await conn.execute(
                    select(t.chats.c.telegram_chat_id).where(t.chats.c.id == gid)
                )
            ).scalar_one()
            policy = Policy.model_validate(
                (
                    await conn.execute(
                        select(t.group_settings.c.policy).where(
                            t.group_settings.c.chat_id == gid
                        )
                    )
                ).scalar_one()
            )
        current = None
        muted = False
        try:
            current = await self.bot.get_chat_member(tgchat, row["telegram_user_id"])
            telegram_status, muted = telegram_member_status(current)
        except Exception:
            telegram_status = (
                "не удалось проверить; сохранённые данные не доказывают текущий бан"
            )
        base = f"v:mp:{gid}:{uid}:{row['version']}"
        ban = (
            ("Разбанить", "unban")
            if str(getattr(current, "status", "")) == "kicked"
            else ("Бан", "ban")
        )
        mute = ("Снять мут", "unmute") if muted else ("Мут на 24 ч", "mute")
        buttons = [
            [
                (
                    "Убрать из вайтлиста" if row["trust"] else "Вайтлист",
                    base + (":trust_remove" if row["trust"] else ":trust_add"),
                ),
                (ban[0], base + ":" + ban[1]),
            ],
            [
                (mute[0], base + ":" + mute[1]),
                ("История", f"v:member_history:{gid}:{uid}"),
            ],
            [("Ещё", f"v:pmore:{gid}:{uid}"), ("Меню", f"v:menu:{gid}")],
        ]
        if scope.binding_version == 0 or scope.role == Role.OBSERVER:
            buttons = [[("История", f"v:member_history:{gid}:{uid}")]]
        await self.send(
            scope.admin_chat_id,
            f"Группа {gid} · {person(row)}\n"
            f"Telegram ID: {row['telegram_user_id']}\nДоверие: {'выдано' if row['trust'] else 'не выдано'} · блэклист: {'да' if row['deny'] else 'нет'}\n"
            f"Сейчас в Telegram: {telegram_status}\n"
            f"Страйки: {row['balance']} (исходный баланс: {row['legacy_balance']})\n"
            f"Сообщений в текущем цикле доверия: {row['credits']} из {policy.auto_whitelist_threshold}; автовайтлист {'включён' if policy.auto_whitelist_enabled else 'выключен'}\n"
            + "\n".join(
                f"{label}: {entry['reason']}; автор {entry['actor_id'] or 'автоматически/legacy'}; до {entry['expires_at'] or 'бессрочно'}"
                for label, entry in [
                    ("Доверие", row.get("trust_entry")),
                    ("Запрет", row.get("deny_entry")),
                ]
                if entry
            )
            + "\n"
            "Текущий бан/мут подтверждается отдельно по Telegram; старый баланс не доказывает наличие бана.",
            keyboard(buttons),
        )

    async def page(self, scope, section, cursor=None, uid=None):
        if (
            section == "roles"
            and scope.binding_version
            and scope.role in {Role.MANAGER, Role.OWNER}
        ):
            return await self.team(scope)
        state = await self.session(scope.actor_id, scope.admin_chat_id)
        filters = (
            state["state"].get("filters", {})
            if state and state["chat_id"] == scope.chat_id
            else {}
        )
        if uid:
            filters = {"user_id": uid}
            await self.save_session(
                scope.actor_id, scope.admin_chat_id, scope.chat_id, filters=filters
            )
        rows, more = await self.operator.page(scope, section, cursor, uid, filters)
        lines, buttons = [f"Группа {scope.chat_id} · {LABELS[section]}"], []
        if filters and section in {"history", "review"}:
            lines.append("Фильтр: " + ", ".join(f"{k}={v}" for k, v in filters.items()))
        for row in rows:
            short = str(row["id"])[:8]
            if section in {"history", "review"}:
                lines.append(
                    f"{short} · {clip(person(row), 55) if row['user_id'] else 'Анонимная публикация'} · {display_label(row['human_label'] or row['model_label'])} · {display_label(row['status'])} · {str(row['created_at'])[:16]}"
                )
                buttons.append(
                    [
                        (
                            f"{clip(person(row), 35) if row['user_id'] else 'Публикация'} · {short}",
                            f"v:card:{scope.chat_id}:{row['id'].hex}",
                        )
                    ]
                )
            elif section in {"trust", "deny"}:
                lines.append(
                    f"{clip(person(row), 60)} · {row['reason'][:100]} · до {row['expires_at'] or 'отзыва'}"
                )
                buttons.append(
                    [
                        (
                            clip(person(row), 50),
                            f"v:user:{scope.chat_id}:{row['user_id']}",
                        )
                    ]
                )
            elif section == "roles":
                lines.append(f"{row['actor_id']} · {row['role']}")
            else:
                lines.append(
                    f"{short} · {row.get('kind')} · {row.get('state', '')} · {row.get('error') or row.get('last_error') or ''}"
                )
                if (
                    section == "actions"
                    and row["state"] in {"failed", "unknown", "in_flight"}
                    and scope.binding_version
                ):
                    buttons.append(
                        [
                            (
                                "Проверить/повторить " + short,
                                f"v:retryaction:{scope.chat_id}:{row['id'].hex}",
                            )
                        ]
                    )
                if section == "jobs" and row["state"] == "dead":
                    buttons.append(
                        [
                            (
                                f"Повторить {short}",
                                f"v:retryjob:{scope.chat_id}:{row['id'].hex}",
                            )
                        ]
                    )
        if not rows:
            lines.append("Записей нет.")
        if more:
            # A cursor cannot exceed Telegram's 64-byte callback limit.
            buttons.append(
                [
                    (
                        "Следующая страница",
                        f"v:page:{scope.chat_id}:{section}:{uuid.UUID(more).hex}",
                    )
                ]
            )
        if section in {"history", "trust", "deny", "actions"} and scope.role in {
            Role.MANAGER,
            Role.OWNER,
        }:
            buttons.append([("Экспорт CSV", f"v:export:{scope.chat_id}:{section}")])
        if section in {"history", "review"}:
            buttons.append(
                [
                    ("Ошибки", f"v:filter:{scope.chat_id}:errors"),
                    ("Рассмотрение", f"v:filter:{scope.chat_id}:review"),
                    ("Проверено", f"v:filter:{scope.chat_id}:checked"),
                    ("В очереди", f"v:filter:{scope.chat_id}:pending"),
                ]
            )
            buttons.append(
                [
                    ("Работа", f"v:filter:{scope.chat_id}:job"),
                    ("Крипто", f"v:filter:{scope.chat_id}:crypto"),
                    ("Инвестиции", f"v:filter:{scope.chat_id}:investment"),
                ]
            )
            buttons.append(
                [
                    ("Фишинг", f"v:filter:{scope.chat_id}:phishing"),
                    ("Другое", f"v:filter:{scope.chat_id}:other"),
                ]
            )
            buttons.append(
                [
                    ("24 часа", f"v:filter:{scope.chat_id}:day"),
                    ("7 дней", f"v:filter:{scope.chat_id}:week"),
                    ("Все", f"v:filter:{scope.chat_id}:all"),
                ]
            )
            buttons.append(
                [
                    ("Скам", f"v:filter:{scope.chat_id}:scam"),
                    ("Не скам", f"v:filter:{scope.chat_id}:safe"),
                ]
            )
        if section == "trust" and scope.binding_version:
            buttons.append([("Импорт CSV", f"v:form:{scope.chat_id}:import")])
        if section == "roles" and scope.binding_version:
            buttons.append([("Изменить роль", f"v:form:{scope.chat_id}:role")])
        buttons.append([("В меню", f"v:menu:{scope.chat_id}")])
        await self.send(scope.admin_chat_id, "\n".join(lines), keyboard(buttons))

    async def stats(self, scope, status=False):
        async with self.engine.connect() as conn:
            await assert_scope(conn, scope, "read")
            settings = (
                (
                    await conn.execute(
                        select(t.group_settings).where(
                            t.group_settings.c.chat_id == scope.chat_id
                        )
                    )
                )
                .mappings()
                .one()
            )
            counts = (
                await conn.execute(
                    select(t.incidents.c.status, func.count())
                    .where(t.incidents.c.chat_id == scope.chat_id)
                    .group_by(t.incidents.c.status)
                )
            ).all()
            usage = (
                await conn.execute(
                    select(func.count(), func.sum(t.checks.c.cost_usd)).where(
                        t.checks.c.chat_id == scope.chat_id
                    )
                )
            ).one()
            queue = (
                await conn.execute(
                    select(t.jobs.c.state, func.count())
                    .where(t.jobs.c.chat_id == scope.chat_id)
                    .group_by(t.jobs.c.state)
                )
            ).all()
            unknown = (
                await conn.execute(
                    select(func.count())
                    .select_from(t.actions)
                    .where(
                        t.actions.c.chat_id == scope.chat_id,
                        t.actions.c.state.in_(["unknown", "in_flight"]),
                    )
                )
            ).scalar_one()

            def valid_tokens(key):
                return case(
                    (
                        func.jsonb_typeof(t.checks.c.usage[key]) == "number",
                        t.checks.c.usage[key].as_float(),
                    ),
                    else_=0,
                )

            metrics = (
                await conn.execute(
                    select(
                        func.sum(valid_tokens("prompt_tokens")),
                        func.sum(valid_tokens("completion_tokens")),
                        func.avg(t.checks.c.latency_ms),
                        func.max(t.checks.c.created_at).filter(
                            t.checks.c.status == "ok"
                        ),
                        func.count().filter(t.checks.c.status == "error"),
                        func.count().filter(t.checks.c.cost_usd.is_(None)),
                    ).where(t.checks.c.chat_id == scope.chat_id)
                )
            ).one()
            skipped = (
                await conn.execute(
                    select(t.checks.c.reason, func.count())
                    .where(
                        t.checks.c.chat_id == scope.chat_id,
                        t.checks.c.status == "skipped",
                    )
                    .group_by(t.checks.c.reason)
                )
            ).all()
            actions = (
                await conn.execute(
                    select(t.actions.c.kind, func.count())
                    .where(
                        t.actions.c.chat_id == scope.chat_id,
                        t.actions.c.state == "succeeded",
                    )
                    .group_by(t.actions.c.kind)
                )
            ).all()
            reviewed = (
                await conn.execute(
                    select(func.count())
                    .select_from(t.decisions)
                    .where(t.decisions.c.chat_id == scope.chat_id)
                )
            ).scalar_one()
            oldest = (
                await conn.execute(
                    select(func.min(t.jobs.c.due_at)).where(
                        t.jobs.c.chat_id == scope.chat_id,
                        t.jobs.c.state.in_(["pending", "running"]),
                        t.jobs.c.due_at <= now(),
                    )
                )
            ).scalar_one()
        await self.send(
            scope.admin_chat_id,
            f"Группа {scope.chat_id}\n{settings['lifecycle']} · {settings['mode']} · {settings['health']}\n"
            f"Причина состояния: {settings['health_reason'] or '—'}\n"
            + "\n".join(f"{k}: {v}" for k, v in counts)
            + f"\nПроверок: {usage[0]}\n"
            + "Очередь: "
            + ", ".join(f"{k} {v}" for k, v in queue)
            + f"\nДействия в исполнении / с неизвестным результатом: {unknown}"
            + f"\nМодель: {getattr(self, 'model_name', None) or 'см. журнал проверок'}\nПоследняя успешная проверка: {metrics[3] or '—'}"
            + f"\nОшибок: {metrics[4]}; средняя задержка API: {round(metrics[2] or 0)} мс; решений человека: {reviewed}"
            + "\nПропуски: "
            + (", ".join(f"{reason}: {count}" for reason, count in skipped) or "нет")
            + "\nВыполнено в Telegram: "
            + (
                ", ".join(f"{kind}: {count}" for kind, count in actions)
                or "нет подтверждённых действий"
            )
            + f"\nСамое старое готовое задание: {oldest or 'нет'}",
            keyboard(
                [
                    [("Открыть очередь", f"v:page:{scope.chat_id}:jobs")],
                    [("В меню", f"v:menu:{scope.chat_id}")],
                ]
            ),
        )

    async def budgets(self, actor, offset=0):
        rows, more = await finance.overview(self.engine, actor, actor, offset)
        global_budget = await finance.global_overview(
            self.engine, actor, actor, getattr(self, "global_budget_default", None)
        )
        await self.save_session(actor, actor)
        text = "Бюджеты проверок · только для владельца\nРасход и резерв за текущие сутки UTC. Валюта учёта — USD.\n"
        limit = global_budget["amount"]
        text += f"\nВесь бот: ${global_budget['used']:.4f} / {f'${limit:g}' if limit is not None else 'без общего лимита'}\n"
        for row in rows:
            limit = row["policy"].get("daily_budget_usd")
            text += f"\n{clip(row['title'], 80)}: ${row['used']:.4f} / {f'${limit:g}' if limit is not None else 'без лимита группы'}"
        text += "\n\nПри исчерпании лимита новые проверки остаются в очереди. Общий лимит и лимиты групп действуют вместе."
        buttons = [[("Общий дневной лимит", "v:gbudget")]] + [
            [(clip(r["title"], 55) + " · изменить лимит", f"v:budgetedit:{r['id']}")]
            for r in rows
        ]
        if more:
            buttons.append([("Дальше", f"v:budgets:{offset + 20}")])
        buttons.append([("В главное меню", "v:system:0")])
        await self.send(actor, text, keyboard(buttons))

    async def global_budget_menu(self, actor):
        value = await finance.global_overview(
            self.engine, actor, actor, getattr(self, "global_budget_default", None)
        )
        await self.save_session(actor, actor, form="global_budget")
        limit = f"${value['amount']:g}" if value["amount"] is not None else "не задан"
        await self.send(
            actor,
            f"💳 Общий дневной лимит: {limit}\nРасход и резерв сегодня: ${value['used']:.4f}.\n\n"
            "Выберите сумму в USD или введите свою. Сутки считаются по UTC. "
            "Изменение применяется после подтверждения, без перезапуска; расход не обнуляется. "
            "Уже начатые проверки могут завершиться. Лимиты групп сохраняются.",
            keyboard(
                [
                    [
                        ("$0.25", "v:gbudgetset:0.25"),
                        ("$1", "v:gbudgetset:1"),
                        ("$5", "v:gbudgetset:5"),
                    ],
                    [("Без общего лимита", "v:gbudgetset:none")],
                    [("Назад", "v:budgets:0")],
                ]
            ),
        )

    async def global_budget_prepare(self, actor, amount):
        token = await finance.prepare_global(
            self.engine,
            actor,
            actor,
            amount,
            getattr(self, "global_budget_default", None),
        )
        await self.save_session(actor, actor)
        caption = (
            f"${amount:g} в сутки UTC"
            if amount is not None
            else "без ограничения общей суммы расходов"
        )
        await self.send(
            actor,
            f"💳 Новый общий лимит: {caption}.\nПодтвердите изменение для всего бота. Лимиты групп сохраняются.",
            keyboard(
                [
                    [("Подтвердить", f"v:gbudgetconfirm:{token}")],
                    [("Отмена", f"v:gbudgetcancel:{token}")],
                ]
            ),
        )

    async def budget_input(self, actor, gid):
        async with self.engine.connect() as conn:
            await finance.authorize(conn, actor, actor)
            title = (
                await conn.execute(select(t.chats.c.title).where(t.chats.c.id == gid))
            ).scalar_one()
        await self.save_session(actor, actor, gid, form="owner_budget", title=title)
        await self.send(
            actor,
            f"Дневной лимит для «{title}»\nВведите сумму в USD, например 0.5, или «нет» для снятия лимита группы. Затем потребуется подтверждение.",
            keyboard([[("Отмена", "v:budgets:0")]]),
        )

    async def system_status(self, actor, offset=0):
        if not await self.auth.is_owner(actor):
            raise Denied("Системная панель доступна владельцу.")
        async with self.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(
                            t.chats.c.id,
                            t.chats.c.title,
                            t.group_settings.c.lifecycle,
                            t.group_settings.c.mode,
                            t.group_settings.c.health,
                        )
                        .join(
                            t.group_settings, t.group_settings.c.chat_id == t.chats.c.id
                        )
                        .order_by(t.chats.c.id)
                        .offset(offset)
                        .limit(26)
                    )
                )
                .mappings()
                .all()
            )
            spend = (
                await conn.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                func.coalesce(t.budget.c.spent, t.budget.c.reserved)
                            ),
                            0,
                        )
                    ).where(t.budget.c.day == now().date().isoformat())
                )
            ).scalar_one()
            state = await controls.flags(conn)
            receiver = (
                await conn.execute(
                    select(t.system.c.updated_at).where(
                        t.system.c.key == "heartbeat:receiver"
                    )
                )
            ).scalar_one_or_none()
        more, rows = len(rows) > 25, rows[:25]
        text = "Системная панель\n" + "\n".join(
            f"{r['title']} · {display_label(r['lifecycle'])} · {display_label(r['mode'])} · {display_label(r['health'])}"
            for r in rows
        )
        received = (
            receiver.strftime("%d.%m.%Y %H:%M:%S UTC")
            if receiver
            else "пока нет данных"
        )
        text += f"\nОбщий расход/резерв сегодня UTC: ${spend:.4f}\nАвтосанкции: {'остановлены' if state.get('automatic_paused') else 'по политике групп'}\nПоследняя связь с Telegram: {received}"
        text += "\n\nДля новой группы нажмите «Подключить пару групп». Все шаги выбора выполняются здесь, в личке. Управление участниками и карточками — в админской группе."
        await self.send(
            actor,
            text,
            keyboard(
                [
                    [("Подключить пару групп", "v:new")],
                    [("Другие подтверждающие", "v:newpeople")],
                    [("Открыть заявку", "v:pending")],
                    [("Бюджеты и расходы", "v:budgets:0")],
                    [("Остановить автосанкции везде", "v:panic:1")],
                    [("Возобновить автосанкции", "v:panic:0")],
                ]
                + [
                    [("История: " + clip(r["title"], 35), f"v:page:{r['id']}:history")]
                    for r in rows
                ]
                + ([[("Следующие группы", f"v:system:{offset + 25}")]] if more else [])
            ),
        )

    async def private_callback(self, actor, parts):
        cmd = parts[1]
        if cmd == "new":
            return await self.show_onboarding(
                actor, await self.onboarding.create(actor)
            )
        if cmd == "newpeople":
            return await self.people_start(actor)
        if cmd == "pending":
            session = await self.session(actor, actor)
            if not session or not session["state"].get("onboarding"):
                return await self.system(actor)
            return await self.show_onboarding(
                actor, uuid.UUID(session["state"]["onboarding"])
            )
        rid, side = uuid.UUID(parts[2]), parts[3] if len(parts) > 3 else None
        if cmd == "pick":
            _, kb = await self.onboarding.selection(actor, rid, side)
            return await self.send(actor, "Выберите группу кнопкой ниже.", kb)
        if cmd == "invite":
            token = await self.onboarding.invite(actor, rid, side)
            me = await self.bot.get_me()
            return await self.send(
                actor,
                f"Передайте персональную ссылку подтверждающему. Срок — 10 минут:\nhttps://t.me/{me.username}?start=link_{token}",
            )
        if cmd == "fallback":
            candidates = await self.onboarding.fallback(actor, rid, side)
            request_id, _ = await self.onboarding.selection(actor, rid, side)
            manual = await self.auth.is_owner(actor)
            if manual:
                await self.save_session(
                    actor,
                    actor,
                    form="group_id",
                    request_id=request_id,
                    onboarding=str(rid),
                )
            return await self.send(
                actor,
                (
                    "Доступные недавно добавленные группы:"
                    if candidates
                    else "Подходящих групп нет. Добавьте бота администратором через настройки Telegram и повторите."
                )
                + (
                    "\nМожно вставить числовой ID группы (обычно начинается с −100). Права будут проверены. Для отмены: /start."
                    if manual
                    else ""
                ),
                keyboard(
                    [
                        [
                            (
                                r["title"] or str(r["telegram_chat_id"]),
                                f"v:select:{request_id}:{r['telegram_chat_id']}",
                            )
                        ]
                        for r in candidates
                    ]
                ),
            )
        if cmd == "connect":
            gid = await self.onboarding.confirm(actor, rid, int(side))
            return await self.send(
                actor,
                f"Связка {gid} подготовлена. После доставки карточки в админскую группу включится наблюдение.",
                keyboard(
                    [
                        [("Проверить подключение", "v:pending")],
                        [("В главное меню", "v:system:0")],
                    ]
                ),
            )

    async def callback(self, query):
        if not query.message or query.from_user.is_bot:
            return
        actor, admin = query.from_user.id, query.message.chat.id
        data = query.data or ""
        panel_command = data.split(":")[1] if data.startswith("v:") else ""
        panel_context = _panel_message.set(
            (admin, query.message.message_id)
            if panel_command in {"sp", "sv", "settingsgroup", "modes", "mv"}
            else None
        )
        try:
            await self.bot.answer_callback_query(query.id)
        except Exception:
            pass
        try:
            if data.startswith("as_"):
                return await self.legacy_callback(actor, admin, data)
            parts = data.split(":")
            if parts[0] != "v":
                return
            cmd = parts[1]
            if await self.people_callback(actor, admin, parts):
                return
            if await self.group_callback(actor, admin, parts):
                return
            if await self.feed_callback(actor, admin, parts):
                return
            if await self.design_private_callback(actor, admin, parts):
                return
            if cmd in {
                "budgets",
                "budgetedit",
                "budgetconfirm",
                "gbudget",
                "gbudgetset",
                "gbudgetconfirm",
                "gbudgetcancel",
            }:
                if actor != admin or not await self.auth.is_owner(actor):
                    raise Denied(
                        "Бюджеты доступны только владельцу бота в личном чате."
                    )
                if cmd == "budgets":
                    return await self.budgets(actor, int(parts[2]))
                if cmd == "budgetedit":
                    return await self.budget_input(actor, int(parts[2]))
                if cmd == "gbudget":
                    return await self.global_budget_menu(actor)
                if cmd == "gbudgetset":
                    return await self.global_budget_prepare(
                        actor, None if parts[2] == "none" else float(parts[2])
                    )
                if cmd == "gbudgetcancel":
                    await finance.cancel_global(self.engine, actor, admin, parts[2])
                    return await self.budgets(actor)
                if cmd == "gbudgetconfirm":
                    result = await finance.confirm_global(
                        self.engine,
                        actor,
                        admin,
                        parts[2],
                        getattr(self, "global_budget_default", None),
                    )
                    return await self.send(
                        actor, result, keyboard([[("К бюджетам", "v:budgets:0")]])
                    )
                result = await finance.confirm(self.engine, actor, admin, parts[2])
                return await self.send(
                    actor, result, keyboard([[("К бюджетам", "v:budgets:0")]])
                )
            if cmd == "onopen":
                async with self.engine.connect() as conn:
                    notice = (
                        (
                            await conn.execute(
                                select(t.outbox).where(
                                    t.outbox.c.key
                                    == "onboarding:" + str(uuid.UUID(parts[2])),
                                    t.outbox.c.destination == admin,
                                )
                            )
                        )
                        .mappings()
                        .first()
                    )
                if not notice:
                    raise Denied("Карточка не принадлежит этой админке.")
                if notice["state"] != "sent":
                    await self.onboarding.acknowledge(
                        actor, admin, parts[2], parts[3], query.message
                    )
                return await self.menu(
                    await self.auth.scope(actor, admin, notice["chat_id"])
                )
            if cmd == "received":
                gid = await self.onboarding.acknowledge(
                    actor, admin, parts[2], parts[3], query.message
                )
                return await self.send(
                    admin,
                    f"Доставка карточки связки {gid} подтверждена. Откройте /as для текущего состояния.",
                )
            if cmd == "system":
                if actor != admin:
                    raise Denied("Системная панель доступна в личке владельца.")
                return await self.system(actor, int(parts[2]))
            if cmd in {"panic", "panicconfirm"}:
                if actor != admin:
                    raise Denied("Аварийное управление доступно в личке владельца.")
                if cmd == "panicconfirm":
                    await controls.confirm(self.engine, actor, parts[2])
                    return await self.send(
                        actor,
                        "Системный режим изменён. Действия, уже отправленные в Telegram, требуют сверки в журнале.",
                    )
                token = await controls.prepare(self.engine, actor, parts[2] == "1")
                return await self.send(
                    actor,
                    "Подтвердите изменение автоматических санкций во всех группах. Старые отменённые действия не будут переотправлены.",
                    keyboard([[("Подтвердить", "v:panicconfirm:" + token)]]),
                )
            if cmd in {"confirm", "cancel"}:
                operation = (
                    self.operator.confirm if cmd == "confirm" else self.operator.cancel
                )
                result = await operation(actor, admin, parts[2])
                return await self.panel(
                    admin,
                    result["message"]
                    + (
                        f"\nВ исполнении/неизвестно: {result['in_flight_or_unknown']}"
                        if result.get("in_flight_or_unknown")
                        else ""
                    ),
                    keyboard(
                        [[("Меню", "v:system:0" if actor == admin else "v:home")]]
                    ),
                    message_id=query.message.message_id,
                )
            if cmd == "select":
                if actor != admin:
                    raise Denied("Подключение доступно в личке.")
                rid = await self.onboarding.share(actor, int(parts[2]), int(parts[3]))
                return await self.show_onboarding(actor, rid)
            if cmd in {
                "new",
                "newpeople",
                "pending",
                "pick",
                "invite",
                "fallback",
                "connect",
            }:
                if actor != admin:
                    raise Denied("Откройте личный чат с ботом.")
                return await self.private_callback(actor, parts)
            if cmd == "choose":
                return await self.choose(
                    actor, admin, int(parts[2]) if len(parts) > 2 else 0
                )
            gid = int(parts[2])
            if actor == admin:
                if not await self.auth.is_owner(actor) or cmd not in {
                    "menu",
                    "card",
                    "content",
                    "images",
                    "decisions",
                    "user",
                    "page",
                    "member_history",
                    "export",
                    "stats",
                    "status",
                    "filter",
                }:
                    raise Denied(
                        "Личные просмотры доступны владельцу. Управление — в связанной админской группе."
                    )
                scope = GroupScope(gid, actor, admin, 0, Role.OWNER)
                if cmd == "menu":
                    return await self.system(actor)
            else:
                scope = await self.auth.scope(actor, admin, gid)
            if await self.design_callback(scope, parts, query):
                return
            if cmd == "menu":
                return await self.menu(scope)
            if cmd in {"members", "memberspage"}:
                query = ""
                if cmd == "memberspage":
                    session = await self.session(actor, admin)
                    if (
                        not session
                        or session["chat_id"] != gid
                        or session["state"].get("form") != "search"
                    ):
                        return await self.search_menu(
                            scope, "Поиск изменился. Начните поиск заново."
                        )
                    query = session["state"].get("member_query", "")
                return await self.participants(scope, query, int(parts[3]))
            if cmd == "card":
                return await self.show_card(scope, parts[3])
            if cmd == "content":
                row = await self.operator.detail(scope, parts[3])
                content = "\n\n".join(
                    r["text"] + "\nOCR:\n" + r["ocr_text"] for r in row["texts"]
                )
                return await self.bot.send_document(
                    admin,
                    BufferedInputFile(
                        content.encode("utf-8"), filename=f"publication-{parts[3]}.txt"
                    ),
                )
            if cmd in {"images", "decisions", "delete"}:
                row = await self.operator.detail(scope, parts[3])
                if cmd == "delete":
                    return await self.confirmation(
                        scope,
                        "delete",
                        {"id": str(row["id"])},
                        row["version"],
                        "Удалить публикацию из этой рабочей группы. Разметка, доверие и страйки не меняются. Удаление исходного сообщения необратимо.",
                    )
                if cmd == "images":
                    files = list(
                        dict.fromkeys(
                            (
                                r["content"].get("image_file_id"),
                                r["content"].get("image_is_document", False),
                            )
                            for r in row["texts"]
                            if r["content"].get("image_file_id")
                        )
                    )
                    if not files:
                        return await self.send(admin, "Сохранённых изображений нет.")
                    for file_id, is_document in files[:10]:
                        try:
                            send_image = (
                                self.bot.send_document
                                if is_document
                                else self.bot.send_photo
                            )
                            await send_image(
                                admin,
                                file_id,
                                caption="Сохранённое изображение выбранной публикации.",
                            )
                        except Exception:
                            await self.send(
                                admin,
                                "Telegram не предоставил изображение. Сохранённый OCR доступен в карточке.",
                            )
                    return
                return await self.send(
                    admin,
                    "История решений (последние 20):\n"
                    + (
                        "\n".join(
                            f"{d['created_at']} · {d['actor_id']} · {d['label']}"
                            for d in row["decisions"]
                        )
                        or "Ручных решений нет."
                    ),
                )
            if cmd == "recheck":
                row = await self.operator.detail(scope, parts[3])
                return await self.confirmation(
                    scope,
                    "retry_check",
                    {"id": str(row["id"])},
                    row["version"],
                    "Повторить модельную проверку текущей редакции. Устаревшая публикация останется для ручного рассмотрения.",
                )
            if cmd == "restore":
                row = await self.operator.detail(scope, parts[3])
                return await self.confirmation(
                    scope,
                    "restore",
                    {"id": str(row["id"]), "reason": "Восстановление после NOT_SCAM"},
                    row["version"],
                    "📄 Опубликовать копию в «"
                    + row["group_title"]
                    + "» от имени бота. Оригинал не восстанавливается.\n\n"
                    + clip("\n".join(item["text"] for item in row["texts"]), 1200),
                )
            if cmd == "user":
                await self.profile(scope, int(parts[3]))
                if scope.binding_version:
                    await self.save_session(actor, admin, gid)
                return
            if cmd == "page":
                return await self.page(
                    scope, parts[3], parts[4] if len(parts) > 4 else None
                )
            if cmd == "filter":
                changes = {
                    "day": {"days": 1},
                    "week": {"days": 7},
                    "all": {},
                    "scam": {"label": "SCAM"},
                    "safe": {"label": "NOT_SCAM"},
                    "errors": {"status": "error"},
                    "review": {"status": "review"},
                    "checked": {"status": "checked"},
                    "pending": {"status": "pending"},
                    "job": {"category": "job_scam"},
                    "crypto": {"category": "crypto"},
                    "investment": {"category": "investment"},
                    "phishing": {"category": "phishing"},
                    "other": {"category": "other"},
                }[parts[3]]
                state = await self.session(actor, admin)
                previous = (
                    state["state"].get("filters", {})
                    if state and state["chat_id"] == gid
                    else {}
                )
                filters = {} if parts[3] == "all" else {**previous, **changes}
                await self.save_session(actor, admin, gid, filters=filters)
                return await self.page(scope, "history")
            if cmd == "member_history":
                return await self.page(scope, "history", uid=int(parts[3]))
            if cmd in {"stats", "status"}:
                return await self.stats(scope, cmd == "status")
            if cmd == "settings":
                return await self.settings(scope, int(parts[3]))
            if cmd == "settingsgroup":
                return await self.settings(scope, section=parts[3])
            if cmd == "export":
                state = await self.session(actor, admin)
                filters = (
                    state["state"].get("filters", {})
                    if state and state["chat_id"] == gid
                    else {}
                )
                data = await self.operator.export(scope, parts[3], filters)
                return await self.bot.send_document(
                    admin,
                    BufferedInputFile(data, filename=f"group-{gid}-{parts[3]}.csv"),
                )
            if cmd == "label":
                row = await self.operator.detail(scope, parts[3])
                if len(parts) < 6:
                    return await self.show_card(scope, row["id"])
                if row["version"] != int(parts[5], 16):
                    raise Conflict(
                        "Решение или публикация изменились. Откройте актуальную карточку."
                    )
                if parts[4] == "NOT_SCAM":
                    await self.execute_quick(
                        scope,
                        "label",
                        {"id": str(row["id"]), "label": "NOT_SCAM", "enforce": False},
                        row["version"],
                    )
                    return

                enforce = parts[4] == "SCAM" and not row["legacy"]
                description = (
                    "Не скам: снять только связанный новый страйк, учесть сообщение в прогрессе доверия. "
                    "Старые/ручные баны не отменяются."
                    if parts[4] == "NOT_SCAM"
                    else "Подтвердить скам. "
                    + (
                        "Удалить публикацию, добавить один страйк и применить пороги группы."
                        if enforce
                        else "Сохранить историческую разметку без автоматических санкций."
                    )
                )
                await self.show_card(scope, row["id"])
                return await self.confirmation(
                    scope,
                    "label",
                    {"id": str(row["id"]), "label": parts[4], "enforce": enforce},
                    row["version"],
                    description,
                )
            if cmd == "member":
                row = await self.operator.profile(scope, int(parts[3]))
                descriptions = {
                    "trust_add": "Выдать локальное доверие (по умолчанию бессрочное). Баланс и ограничения Telegram сохраняются.",
                    "trust_remove": "Снять локальное доверие и начать новый цикл; старые сообщения его не наполнят.",
                    "deny_add": "Включить локальный блэклист. Новые публикации будут удаляться в автоматическом режиме; это не бан и не снятие доверия.",
                    "deny_remove": "Убрать локальный блэклист. Само по себе не снимает бан или мут.",
                    "ban": "Забанить участника только в этой группе.",
                    "unban": "Снять бан только здесь. Блэклист нужно удалить отдельно.",
                    "mute": "Ограничить отправку сообщений на срок из настроек группы.",
                    "unmute": "Снять только подтверждённое ограничение этого бота с учётом текущих прав группы.",
                    "reset_strikes": "Обнулить локальный баланс. Не снимать доверие, бан или мут.",
                }
                scope.require("trust" if parts[4].startswith("trust") else "moderate")
                await self.save_session(
                    actor,
                    admin,
                    gid,
                    form="member_action",
                    action=parts[4],
                    user_id=row["user_id"],
                    object_version=row["version"],
                    description=descriptions[parts[4]],
                    days=None,
                )
                options = (
                    [
                        [(REASONS[key], f"v:reason:{gid}:{key}")]
                        for key in ACTION_REASONS[parts[4]]
                    ]
                    if parts[4] != "trust_add"
                    else []
                )
                if parts[4] in {"trust_add", "deny_add"}:
                    options.append(
                        [
                            ("Бессрочно", f"v:duration:{gid}:0"),
                            ("На 7 дней", f"v:duration:{gid}:7"),
                            ("На 30 дней", f"v:duration:{gid}:30"),
                        ]
                    )
                if parts[4] == "mute":
                    options.append(
                        [
                            ("1 час", f"v:mutetime:{gid}:3600"),
                            ("24 часа", f"v:mutetime:{gid}:86400"),
                            ("7 дней", f"v:mutetime:{gid}:604800"),
                        ]
                    )
                    options.append([("Другой срок", f"v:mutetime:{gid}:custom")])
                options.append(
                    [("Отмена — к профилю", f"v:user:{gid}:{row['user_id']}")]
                )
                return await self.send(
                    admin,
                    (
                        "Добавить участника в вайтлист этой группы.\nВыберите срок; затем подтвердите."
                        if parts[4] == "trust_add"
                        else descriptions[parts[4]]
                        + "\nУкажите причину текстом или выберите ниже; затем будет подтверждение."
                    ),
                    keyboard(options),
                )
            if cmd in {"reason", "duration", "mutetime"}:
                session = await self.session(actor, admin)
                if (
                    not session
                    or session["chat_id"] != gid
                    or session["state"].get("form") != "member_action"
                ):
                    raise Conflict(
                        "Откройте действие в своём профиле участника заново."
                    )
                state = session["state"]
                if cmd == "mutetime":
                    if state["action"] != "mute":
                        raise Conflict("Откройте действие «Мут» заново.")
                    if parts[3] == "custom":
                        state["form"] = "mute_duration"
                        await self.save_session(actor, admin, gid, **state)
                        return await self.send(
                            admin,
                            "Пришлите срок в минутах: от 1 до 527040. Затем укажите причину.",
                        )
                    state["seconds"] = int(parts[3])
                    await self.save_session(actor, admin, gid, **state)
                    return await self.send(
                        admin,
                        f"Срок: {state['seconds'] // 60} минут. Пришлите причину или выберите её в предыдущем сообщении.",
                    )
                if cmd == "duration":
                    if state["action"] not in {"trust_add", "deny_add"}:
                        raise Conflict(
                            "Для этого действия срок выбирается в настройках группы."
                        )
                    if parts[3] not in {"0", "7", "30"}:
                        raise Conflict("Выберите срок одной из кнопок.")
                    state["days"] = int(parts[3]) or None
                    if state["action"] == "trust_add":
                        return await self.member_confirmation(
                            scope, state, REASONS["trusted"]
                        )
                    await self.save_session(actor, admin, gid, **state)
                    return await self.send(
                        admin,
                        "Срок: "
                        + (
                            str(state["days"]) + " дней"
                            if state["days"]
                            else "бессрочно"
                        )
                        + ". Теперь пришлите причину.",
                    )
                if parts[3] not in ACTION_REASONS.get(state["action"], ()):
                    raise Conflict(
                        "Эта причина не подходит к выбранному действию. Откройте действие в профиле заново."
                    )
                reason = REASONS[parts[3]]
                return await self.member_confirmation(scope, state, reason)
            if cmd == "retryjob":
                return await self.confirmation(
                    scope,
                    "retry_job",
                    {"id": parts[3]},
                    description="Повторить завершившееся ошибкой задание с актуальными проверками прав.",
                )
            if cmd == "retryaction":
                return await self.confirmation(
                    scope,
                    "retry_action",
                    {"id": parts[3]},
                    description="Известный отказ можно повторить с актуальными правами. При неизвестном результате будет только сверка, без повторной отправки.",
                )
            if cmd == "modes":
                return await self.mode_panel(scope)
            if cmd == "mode":
                return await self.confirmation(
                    scope,
                    "mode",
                    {"mode": parts[3]},
                    description="Установить режим: "
                    + parts[3]
                    + ". Уже отправленные Telegram-действия могут завершиться; их статус виден в журнале.",
                )
            if cmd in {"form", "setting"}:
                key = parts[3]
                if cmd == "setting" and key == "daily_budget_usd":
                    raise Denied(
                        "Бюджеты доступны владельцу бота в личной панели «Бюджеты и расходы»."
                    )
                if cmd == "form" and key == "search":
                    return await self.search_menu(scope)
                permission = (
                    "settings"
                    if cmd == "setting" or key == "import"
                    else "roles"
                    if key == "role"
                    else "read"
                )
                scope.require(permission)
                await self.save_session(
                    actor,
                    admin,
                    gid,
                    form="setting" if cmd == "setting" else key,
                    field=key,
                )
                prompts = {
                    "search": "Пришлите Telegram ID или @username участника.",
                    "role": "Пришлите Telegram ID и роль: менеджер, модератор, наблюдатель или удалить.",
                    "import": "Пришлите CSV до 2 МБ, до 10 000 строк: telegram_user_id,reason. Сначала покажу объём для подтверждения.",
                }
                return await self.send(
                    admin,
                    (setting_prompt(key) if cmd == "setting" else prompts[key]),
                    keyboard([[("В меню", f"v:menu:{gid}")]]),
                )
        except (Denied, Conflict, ValueError, KeyError, IndexError) as exc:
            await self.send(admin, clip(str(exc), 1000))
            return
        finally:
            _panel_message.reset(panel_context)
            try:
                await self.bot.answer_callback_query(query.id)
            except Exception:
                pass

    async def message(self, message):
        if not message.from_user or message.from_user.is_bot or message.sender_chat:
            return False
        actor, admin = message.from_user.id, message.chat.id
        value = message.text or ""
        private = str(message.chat.type) == "private"
        try:
            if private:
                if await self.people_message(message):
                    return True
                if value.startswith("/start team_"):
                    await self.accept_team_invite(
                        message.from_user, value.split("team_", 1)[1].strip()
                    )
                    return True
                if message.chat_shared:
                    rid = await self.onboarding.share(
                        actor,
                        message.chat_shared.request_id,
                        message.chat_shared.chat_id,
                    )
                    await self.send(actor, "Группа проверена.", ReplyKeyboardRemove())
                    await self.show_onboarding(actor, rid)
                    return True
                if value.startswith("/start link_"):
                    _, kb = await self.onboarding.accept(
                        actor, value.split("link_", 1)[1].strip()
                    )
                    await self.send(actor, "Подтвердите группу кнопкой.", kb)
                    return True
                session = await self.session(actor, admin)
                if (
                    session
                    and session["state"].get("form") == "group_id"
                    and not value.startswith("/")
                ):
                    if not await self.auth.is_owner(actor):
                        raise Denied("Ручной ввод группы доступен владельцу.")
                    chat_id = int(value.strip().replace("−", "-"))
                    if not -(2**52) < chat_id < 0:
                        raise Conflict("Нужен отрицательный числовой ID группы.")
                    rid = await self.onboarding.share(
                        actor, session["state"]["request_id"], chat_id
                    )
                    await self.send(actor, "Группа проверена.", ReplyKeyboardRemove())
                    await self.show_onboarding(actor, rid)
                    return True
                if (
                    session
                    and session["state"].get("form") == "global_budget"
                    and not value.startswith("/")
                ):
                    amount = (
                        None
                        if value.strip().lower() == "нет"
                        else float(value.strip().replace(",", "."))
                    )
                    await self.global_budget_prepare(actor, amount)
                    return True
                if (
                    session
                    and session["state"].get("form") == "application_note"
                    and not value.startswith("/")
                ):
                    state = session["state"]
                    await self.onboarding.review(
                        actor,
                        state["request"],
                        state["version"],
                        state["decision"],
                        value,
                    )
                    await self.show_onboarding(actor, state["request"])
                    return True
                if (
                    session
                    and session["state"].get("form") == "owner_budget"
                    and not value.startswith("/")
                ):
                    amount = (
                        None
                        if value.strip().lower() == "нет"
                        else float(value.strip().replace(",", "."))
                    )
                    token = await finance.prepare(
                        self.engine, actor, admin, session["chat_id"], amount
                    )
                    await self.save_session(actor, actor)
                    await self.send(
                        actor,
                        f"Лимит для «{session['state']['title']}»: {f'${amount:g} в сутки UTC' if amount is not None else 'без лимита группы'}.\nПодтвердите изменение. Общий лимит бота сохраняется.",
                        keyboard(
                            [
                                [("Подтвердить", f"v:budgetconfirm:{token}")],
                                [("Отмена", f"v:cancel:{token}")],
                            ]
                        ),
                    )
                    return True
                await self.system(actor)
                return True
            async with self.engine.connect() as conn:
                is_admin = (
                    await conn.execute(
                        select(t.bindings.c.id).where(
                            t.bindings.c.admin_chat_id == admin,
                            t.bindings.c.closed_at.is_(None),
                        )
                    )
                ).first()
            if not is_admin:
                async with self.engine.begin() as conn:
                    pending_admin = (
                        await conn.execute(
                            select(t.onboarding.c.id)
                            .where(
                                t.onboarding.c.admin_chat_id == admin,
                                t.onboarding.c.admin_confirmed.is_(True),
                                t.onboarding.c.state.in_(
                                    ["pending", "submitted", "awaiting_delivery"]
                                ),
                            )
                            .limit(1)
                        )
                    ).first()
                    if pending_admin and value.split("@")[0].strip() == "/as":
                        await remember_contact(conn, admin, message.from_user)
                if pending_admin and value.split("@")[0].strip() == "/as":
                    await self.send(
                        admin,
                        "👤 Участник зарегистрирован для выбора управляющего. Подключение ещё не завершено; доступ пока не выдан.",
                    )
                    return True
                return False
            async with self.engine.begin() as conn:
                await remember_contact(conn, admin, message.from_user)
            command = value.split(maxsplit=1)[0].split("@")[0] if value else ""
            if command in {"/as_set_admin_chat", "/as_reactivate", "/as_deactivated"}:
                if not await self.auth.is_owner(actor):
                    raise Denied(
                        "Подключение и архив доступны владельцу в личном чате."
                    )
                me = await self.bot.get_me()
                await self.send(
                    admin,
                    "Откройте личную панель владельца. Для смены привязки выберите в мастере существующую рабочую и новую админскую группу; история сохранится.",
                    InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="Личная панель",
                                    url=f"https://t.me/{me.username}?start=system",
                                )
                            ]
                        ]
                    ),
                )
                return True
            if command in {
                "/as",
                "/cancel",
                "/start",
                "/help",
                "/as_help",
                "/as_set_admin_chat",
                "/as_reactivate",
                "/as_deactivated",
            }:
                await self.choose(actor, admin)
                return True
            command_routes = {
                "/history": ["v", "feed", "history", "0"],
                "/members": ["v", "target", "members"],
                "/settings": ["v", "target", "settings"],
            }
            if command in command_routes:
                await self.feed_callback(actor, admin, command_routes[command])
                return True
            session = await self.session(actor, admin)
            if (
                session
                and session["state"].get("form") == "feedsearch"
                and not value.startswith("/")
            ):
                filters = {
                    **session["state"].get("feed_filters", {}),
                    "query": value.strip()[:100],
                }
                await self.save_session(actor, admin, feed_filters=filters)
                await self.feed(actor, admin)
                return True
            if not session or not session["chat_id"]:
                groups = await self.auth.groups(actor, admin)
                if len(groups) != 1:
                    if value.startswith("/") or message.forward_origin:
                        await self.choose(actor, admin)
                    return True
                await self.save_session(actor, admin, groups[0]["id"])
                session = await self.session(actor, admin)
            scope = await self.auth.scope(actor, admin, session["chat_id"])
            if command == "/chatid":
                await self.send(
                    admin,
                    f"Админская группа: {admin}. Рабочая группа в панели: {scope.chat_id}.",
                )
                return True
            aliases = {
                "/as_recent": "history",
                "/as_stats": "stats",
                "/as_status": "status",
            }
            if command in aliases:
                if command == "/as_recent":
                    await self.page(scope, "history")
                else:
                    await self.stats(scope, command == "/as_status")
                return True
            old = {
                "/as_wl": "trust_add",
                "/as_rs": "reset_strikes",
                "/as_unban": "unban",
                "/as_unmute": "unmute",
                "/as_ban": "ban",
                "/as_user": "profile",
            }
            form = session["state"].get("form")
            if message.forward_origin:
                origin = message.forward_origin
                if origin.type != "user":
                    await self.search_menu(
                        scope,
                        "Telegram не передал профиль автора этой пересылки. "
                        "Автоматически определять его по имени нельзя. "
                        "Выберите человека из участников или откройте исходное сообщение в истории.",
                    )
                    return True
                value, form = str(origin.sender_user.id), "search"
            if command in old:
                parts = value.split(maxsplit=1)
                if len(parts) != 2:
                    raise Conflict("Укажите Telegram ID участника после команды.")
                value, form = parts[1], "search"
            if form == "teamsearch":
                await self.team(scope, candidates=True, query=value.strip())
                return True
            if form == "search":
                value = value.strip()
                if not (value.isascii() and value.isdigit()):
                    await self.participants(scope, value)
                    return True
                if not 0 < int(value) <= 9223372036854775807:
                    raise Conflict("Некорректный Telegram ID.")
                async with self.engine.connect() as conn:
                    condition = (
                        t.users.c.telegram_user_id == int(value)
                        if value.lstrip("-").isdigit()
                        else func.lower(t.users.c.username) == value.lstrip("@").lower()
                    )
                    found_rows = (
                        await conn.execute(
                            select(t.users.c.id)
                            .join(
                                t.memberships, t.memberships.c.user_id == t.users.c.id
                            )
                            .where(t.memberships.c.chat_id == scope.chat_id, condition)
                            .limit(2)
                        )
                    ).all()
                    if len(found_rows) > 1:
                        raise Conflict(
                            "Username соответствует нескольким старым записям. Укажите числовой Telegram ID."
                        )
                    row = found_rows[0] if found_rows else None
                if not row:
                    if not value.isdigit() or int(value) <= 0:
                        raise Conflict(
                            "Участник не найден в этой группе. Для новой записи нужен числовой Telegram ID."
                        )
                    scope.require("moderate")
                    async with self.engine.connect() as conn:
                        work = (
                            await conn.execute(
                                select(t.chats.c.telegram_chat_id).where(
                                    t.chats.c.id == scope.chat_id
                                )
                            )
                        ).scalar_one()
                    try:
                        found = await self.bot.get_chat_member(work, int(value))
                    except Exception as exc:
                        raise Conflict(
                            "Telegram не подтвердил участника в этой группе."
                        ) from exc
                    if str(found.status) == "left":
                        raise Conflict("Участник не найден в этой группе.")
                    async with self.engine.begin() as conn:
                        await assert_scope(conn, scope, "moderate")
                        uid = await ensure_user(conn, int(value))
                        await ensure_member(conn, scope.chat_id, uid)
                    row = (uid,)
                await self.profile(scope, row[0])
                if command in old and old[command] != "profile":
                    profile = await self.operator.profile(scope, row[0])
                    await self.confirmation(
                        scope,
                        old[command],
                        {"user_id": row[0]},
                        profile["version"],
                        "Локальное действие: "
                        + old[command]
                        + ". Подтвердите только после проверки профиля выше.",
                    )
            elif form == "mute_custom":
                seconds = int(value) * 60
                if not 60 <= seconds <= 366 * 86400:
                    raise Conflict("Срок должен быть от 1 до 527040 минут.")
                await self.member_action(
                    scope,
                    session["state"]["user_id"],
                    "mute",
                    session["state"]["object_version"],
                    seconds=seconds,
                )
                await self.save_session(actor, admin, scope.chat_id)
                return True
            elif form == "mute_duration":
                seconds = int(value) * 60
                if not 60 <= seconds <= 366 * 86400:
                    raise Conflict("Срок должен быть от 1 до 527040 минут.")
                state = {
                    **session["state"],
                    "form": "member_action",
                    "seconds": seconds,
                }
                await self.save_session(actor, admin, scope.chat_id, **state)
                await self.send(admin, "Срок сохранён. Теперь пришлите причину.")
                return True
            elif form == "member_action":
                await self.member_confirmation(scope, session["state"], value)
            elif form == "role":
                uid, role = value.split()
                role = {
                    "менеджер": "manager",
                    "модератор": "moderator",
                    "наблюдатель": "observer",
                    "удалить": "remove",
                }.get(role.lower(), role)
                await self.confirmation(
                    scope,
                    "role",
                    {"actor_id": int(uid), "role": role},
                    description=f"Изменить локальную роль {uid}: {role}",
                )
            elif form == "setting":
                field = session["state"]["field"]
                if field == "daily_budget_usd":
                    raise Denied(
                        "Бюджеты меняет владелец бота в личной панели. Эта форма устарела."
                    )
                try:
                    parsed = json.loads(value)
                except ValueError:
                    parsed = value
                if value.lower() in {"вкл", "выкл", "нет"}:
                    parsed = {"вкл": True, "выкл": False, "нет": None}[value.lower()]
                changes = {field: parsed}
                if field in {"quiet_start", "quiet_end"}:
                    if parsed is None:
                        changes = {"quiet_start": None, "quiet_end": None}
                    elif len(value.split()) == 2:
                        changes = dict(zip(["quiet_start", "quiet_end"], value.split()))
                if field == "categories" and not isinstance(parsed, list):
                    changes[field] = [v.strip() for v in value.split(",") if v.strip()]
                async with self.engine.connect() as conn:
                    policy = (
                        await conn.execute(
                            select(t.group_settings.c.policy).where(
                                t.group_settings.c.chat_id == scope.chat_id
                            )
                        )
                    ).scalar_one()
                validated = Policy.model_validate({**policy, **changes})
                if "expected_policy_version" in session["state"]:
                    await self.apply_setting(
                        scope, changes, session["state"]["expected_policy_version"]
                    )
                    return True
                await self.confirmation(
                    scope,
                    "settings",
                    {"changes": {key: getattr(validated, key) for key in changes}},
                    description=f"{SETTINGS[field]}: {policy_value(field, validated.model_dump())}",
                )
            elif form == "import" and message.document:
                if (message.document.file_size or 0) > 2 * 1024 * 1024:
                    raise Conflict("Файл превышает 2 МБ.")

                class Limited(io.BytesIO):
                    def write(self, data):
                        if self.tell() + len(data) > 2 * 1024 * 1024:
                            raise Conflict("Файл превышает 2 МБ.")
                        return super().write(data)

                data = Limited()
                await self.bot.download(message.document.file_id, destination=data)
                rows = parse_import(data.getvalue())
                await self.confirmation(
                    scope,
                    "import_trust",
                    {"rows": rows},
                    description=f"Добавить доверие {len(rows)} участникам только этой группы. Существующие записи и баны сохраняются.",
                )
            else:
                return True
            await self.save_session(actor, admin, scope.chat_id)
        except (Denied, Conflict, ValueError, KeyError) as exc:
            await self.send(admin, str(exc))
        return True

    async def member_confirmation(self, scope, state, reason):
        if not 3 <= len(reason.strip()) <= 1000:
            raise Conflict("Причина: от 3 до 1000 символов.")
        await self.confirmation(
            scope,
            state["action"],
            {
                "user_id": state["user_id"],
                "reason": reason.strip(),
                "days": state.get("days"),
                **({"seconds": state["seconds"]} if "seconds" in state else {}),
            },
            state["object_version"],
            (
                "Добавить в вайтлист этой группы."
                if state["action"] == "trust_add"
                else state["description"] + "\nПричина: " + reason.strip()
            )
            + (
                "\nСрок: "
                + (str(state["days"]) + " дней" if state.get("days") else "бессрочно")
                if state["action"] in {"trust_add", "deny_add"}
                else ""
            )
            + (
                "\nСрок мута: " + str(state["seconds"] // 60) + " минут"
                if "seconds" in state
                else ""
            ),
        )
        await self.save_session(scope.actor_id, scope.admin_chat_id, scope.chat_id)

    async def legacy_callback(self, actor, admin, data):
        name, value = data.split(":", 1)
        if name not in {"as_not_scam", "as_mark_scam"}:
            groups = await self.auth.groups(actor, admin)
            if len(groups) != 1 or name not in {
                "as_u_wl",
                "as_u_rs",
                "as_u_unban",
                "as_u_ban",
            }:
                return await self.choose(actor, admin)
            scope = await self.auth.scope(actor, admin, groups[0]["id"])
            async with self.engine.connect() as conn:
                uid = (
                    await conn.execute(
                        select(t.users.c.id)
                        .join(t.memberships, t.memberships.c.user_id == t.users.c.id)
                        .where(
                            t.memberships.c.chat_id == scope.chat_id,
                            t.users.c.telegram_user_id == int(value),
                        )
                    )
                ).scalar_one_or_none()
            if not uid:
                raise Conflict("Участник старой карточки не найден в этой группе.")
            await self.profile(scope, uid)
            profile = await self.operator.profile(scope, uid)
            kind = {
                "as_u_wl": "trust_add",
                "as_u_rs": "reset_strikes",
                "as_u_unban": "unban",
                "as_u_ban": "ban",
            }[name]
            return await self.confirmation(
                scope,
                kind,
                {"user_id": uid},
                profile["version"],
                "Действие из старой карточки: "
                + kind
                + ". Оно применяется только в выбранной рабочей группе.",
            )
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(
                        t.legacy_links.c.chat_id, t.legacy_links.c.incident_id
                    ).where(t.legacy_links.c.legacy_message_id == int(value))
                )
            ).first()
        if not row:
            raise Conflict("Старая карточка не найдена. Откройте историю.")
        scope = await self.auth.scope(actor, admin, row.chat_id)
        await self.show_card(scope, row.incident_id)
