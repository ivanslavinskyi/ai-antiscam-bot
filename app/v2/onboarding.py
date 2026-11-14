"""Private onboarding. This module never sends anything to a working group."""

import hashlib
import secrets
import uuid
from datetime import timedelta

from aiogram.types import (
    ChatAdministratorRights,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ReplyKeyboardMarkup,
)
from sqlalchemy import select, func, text

from app.v2 import tables as t
from app.v2.auth import owner
from app.v2.domain import Conflict, Denied, Policy, now
from app.v2.queue import emit
from app.v2.state import audit


def rights(side, user=False):
    values = {name: False for name in ChatAdministratorRights.model_fields}
    values.update(can_manage_chat=True, can_invite_users=user, can_promote_members=user)
    if side == "work":
        values.update(can_delete_messages=True, can_restrict_members=True)
    return ChatAdministratorRights(**values)


class Onboarding:
    def __init__(self, engine, bot, auth):
        self.engine, self.bot, self.auth = engine, bot, auth

    async def create(
        self,
        actor,
        manager=None,
        work_actor=None,
        admin_actor=None,
        *,
        people_draft=None,
    ):
        async with self.engine.begin() as conn:
            is_owner = await owner(conn, actor)
            if actor <= 0:
                raise Denied("Нужен личный аккаунт администратора.")
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": actor}
            )
            draft = None
            if people_draft is not None:
                if not is_owner:
                    raise Denied("Назначение других подтверждающих выполняет владелец.")
                draft = (
                    await conn.execute(
                        select(t.sessions.c.state)
                        .where(
                            t.sessions.c.actor_id == actor,
                            t.sessions.c.admin_chat_id == actor,
                            t.sessions.c.expires_at > now(),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    not draft
                    or draft.get("form") != "newpeople"
                    or draft.get("draft") != people_draft
                ):
                    raise Conflict("Выбор людей устарел. Начните заново.")
                if draft.get("created_request"):
                    return uuid.UUID(draft["created_request"])
                if [
                    draft["people"][side]["id"] for side in ("manager", "work", "admin")
                ] != [manager, work_actor, admin_actor]:
                    raise Conflict(
                        "Состав изменился. Проверьте выбранных людей заново."
                    )
            active = (
                await conn.execute(
                    select(func.count())
                    .select_from(t.onboarding)
                    .where(
                        t.onboarding.c.owner_id == actor,
                        t.onboarding.c.state.in_(
                            ["pending", "submitted", "awaiting_delivery"]
                        ),
                        t.onboarding.c.durable.is_(True),
                    )
                )
            ).scalar_one()
            if active >= 3 and not is_owner:
                raise Conflict("У вас уже три открытые заявки. Откройте «Мои заявки».")
            if not is_owner and any(
                v is not None and v != actor for v in (manager, work_actor, admin_actor)
            ):
                raise Denied("Назначение других подтверждающих выполняет владелец.")
            if any(
                v is not None and v <= 0 for v in (manager, work_actor, admin_actor)
            ):
                raise Conflict("Нужны положительные Telegram ID пользователей.")
            rid = uuid.uuid4()
            await conn.execute(
                t.onboarding.insert().values(
                    id=rid,
                    owner_id=actor,
                    durable=True,
                    approval_required=not is_owner,
                    manager_id=manager or actor,
                    work_actor_id=work_actor or actor,
                    admin_actor_id=admin_actor or actor,
                    expires_at=now() + timedelta(minutes=10),
                )
            )
            if draft is not None:
                await conn.execute(
                    t.sessions.update()
                    .where(
                        t.sessions.c.actor_id == actor,
                        t.sessions.c.admin_chat_id == actor,
                    )
                    .values(state={**draft, "created_request": str(rid)})
                )
        return rid

    async def applications(self, actor, offset=0):
        async with self.engine.connect() as conn:
            query = (
                select(t.onboarding)
                .order_by(t.onboarding.c.created_at.desc())
                .offset(max(0, offset))
                .limit(20)
            )
            if not await owner(conn, actor):
                query = query.where(t.onboarding.c.owner_id == actor)
            return [dict(row) for row in (await conn.execute(query)).mappings()]

    async def submit(self, actor, rid, version):
        row = await self.get(actor, rid)
        if (
            actor != row["owner_id"]
            or not row["approval_required"]
            or row["state"] != "pending"
        ):
            raise Denied("Заявку отправляет её заявитель.")
        if not row["work_confirmed"] or not row["admin_confirmed"]:
            raise Conflict("Выберите обе группы и проверьте права.")
        work = await self.verify(row["work_chat_id"], row["work_actor_id"], "work")
        admin = await self.verify(row["admin_chat_id"], row["admin_actor_id"], "admin")
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT pg_advisory_xact_lock(71392145)"))
            current = (
                (
                    await conn.execute(
                        select(t.onboarding)
                        .where(t.onboarding.c.id == row["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if current["version"] != version or current["state"] != "pending":
                raise Conflict("Заявка изменилась. Откройте её заново.")
            existing = (
                await conn.execute(
                    select(t.group_settings.c.chat_id)
                    .join(t.aliases, t.aliases.c.chat_id == t.group_settings.c.chat_id)
                    .where(
                        t.aliases.c.telegram_chat_id == row["work_chat_id"],
                        t.group_settings.c.lifecycle.in_(["connected", "pending"]),
                    )
                )
            ).first()
            if existing:
                raise Conflict(
                    "Для этой группы уже есть подключение. Обратитесь к владельцу для смены связи."
                )
            duplicate = (
                await conn.execute(
                    select(t.onboarding.c.id).where(
                        t.onboarding.c.work_chat_id == row["work_chat_id"],
                        t.onboarding.c.id != row["id"],
                        t.onboarding.c.state.in_(["submitted", "awaiting_delivery"]),
                    )
                )
            ).first()
            if duplicate:
                raise Conflict("Для этой рабочей группы уже рассматривается заявка.")
            await conn.execute(
                t.onboarding.update()
                .where(t.onboarding.c.id == row["id"])
                .values(state="submitted", review_note=None)
            )
            owners = (
                (
                    await conn.execute(
                        select(t.roles.c.actor_id).where(
                            t.roles.c.role == "owner",
                            t.roles.c.chat_id.is_(None),
                            t.roles.c.revoked_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for recipient in owners:
                await emit(
                    conn,
                    destination=recipient,
                    kind="application",
                    payload={
                        "request": str(row["id"]),
                        "owner_only": True,
                        "text": f"📨 Новая заявка на подключение\n👥 {work.title} → {admin.title}\nЗаявитель: {actor}",
                    },
                    key=f"application:{row['id']}:{version}:{recipient}",
                )
            await audit(
                conn,
                None,
                actor,
                "application.submitted",
                request=str(row["id"]),
                version=version,
            )

    async def review(self, actor, rid, version, decision, note=""):
        row = await self.get(actor, rid)
        if decision not in {"revise", "reject", "withdraw"}:
            raise Conflict("Неизвестное решение.")
        async with self.engine.begin() as conn:
            current = (
                (
                    await conn.execute(
                        select(t.onboarding)
                        .where(t.onboarding.c.id == row["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            allowed = (
                actor == row["owner_id"]
                if decision == "withdraw"
                else await owner(conn, actor)
            )
            if not allowed:
                raise Denied("Нет полномочий для этого решения.")
            if current["version"] != version or current["state"] not in {
                "pending",
                "submitted",
            }:
                raise Conflict("Заявка уже изменилась.")
            state = {
                "revise": "pending",
                "reject": "rejected",
                "withdraw": "withdrawn",
            }[decision]
            await conn.execute(
                t.onboarding.update()
                .where(t.onboarding.c.id == row["id"])
                .values(
                    state=state,
                    version=version + 1,
                    review_note=note[:1000],
                    approved_by=None,
                    approved_version=None,
                )
            )
            await audit(
                conn,
                None,
                actor,
                "application." + decision,
                request=str(row["id"]),
                version=version,
            )
            if decision != "withdraw":
                await emit(
                    conn,
                    destination=row["owner_id"],
                    kind="application",
                    payload={
                        "request": str(row["id"]),
                        "text": (
                            "🛠 Заявку нужно исправить"
                            if decision == "revise"
                            else "❌ Заявка отклонена"
                        )
                        + "\n"
                        + note[:1000],
                    },
                    key=f"application:{row['id']}:{version}:{decision}",
                )

    async def set_manager(self, actor, rid, version, candidate):
        row = await self.get(actor, rid)
        if row["owner_id"] != actor or not await self.auth.is_owner(actor):
            raise Denied(
                "Выбор другого управляющего доступен владельцу в своём мастере."
            )
        if not row["admin_chat_id"]:
            raise Conflict("Сначала выберите админскую группу.")
        member = await self.bot.get_chat_member(row["admin_chat_id"], candidate)
        if str(member.status) not in {"creator", "administrator", "member"}:
            raise Denied("Кандидат должен состоять в выбранной админской группе.")
        async with self.engine.begin() as conn:
            if not await owner(conn, actor):
                raise Denied("Роль владельца отозвана.")
            updated = (
                await conn.execute(
                    t.onboarding.update()
                    .where(
                        t.onboarding.c.id == row["id"],
                        t.onboarding.c.owner_id == actor,
                        t.onboarding.c.state == "pending",
                        t.onboarding.c.version == version,
                    )
                    .values(manager_id=candidate, version=version + 1)
                    .returning(t.onboarding.c.id)
                )
            ).scalar_one_or_none()
            if not updated:
                raise Conflict("Заявка изменилась. Откройте её заново.")

    async def configure(self, actor, rid, version, changes):
        allowed = {
            "auto_whitelist_enabled",
            "auto_whitelist_threshold",
            "exempt_admins",
        }
        if not changes or set(changes) - allowed:
            raise Denied("В заявке доступны только начальные настройки доверия.")
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        select(t.onboarding)
                        .where(t.onboarding.c.id == uuid.UUID(str(rid)))
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if not row or row["owner_id"] != actor:
                raise Denied("Заявка недоступна.")
            if row["state"] != "pending" or row["version"] != version:
                raise Conflict("Заявка изменилась. Откройте её заново.")
            policy = Policy.model_validate(
                {**row["initial_policy"], **changes}
            ).model_dump()
            await conn.execute(
                t.onboarding.update()
                .where(t.onboarding.c.id == row["id"])
                .values(initial_policy=policy, version=version + 1)
            )

    async def get(self, actor, rid):
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(t.onboarding).where(
                            t.onboarding.c.id == uuid.UUID(str(rid))
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not row or (
                actor
                not in {
                    row["owner_id"],
                    row["work_actor_id"],
                    row["admin_actor_id"],
                }
                and not await owner(conn, actor)
            ):
                raise Denied("Заявка недоступна.")
            if not row["durable"] and row["expires_at"] <= now():
                raise Conflict("Заявка истекла. Начните подключение заново.")
            if not row["approval_required"] and not await owner(conn, row["owner_id"]):
                raise Denied("Полномочия владельца отозваны.")
            return dict(row)

    async def selection(self, actor, rid, side):
        row = await self.get(actor, rid)
        if (
            side not in {"work", "admin"}
            or row[side + "_actor_id"] != actor
            or row["state"] != "pending"
        ):
            raise Denied("Этот этап предназначен другому участнику.")
        request_id = secrets.randbelow(2**31 - 1)
        async with self.engine.begin() as conn:
            await conn.execute(
                t.selections.insert().values(
                    request_id=request_id,
                    request=row["id"],
                    actor_id=actor,
                    side=side,
                    expires_at=now() + timedelta(minutes=10),
                )
            )
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text="Выбрать рабочую группу"
                        if side == "work"
                        else "Выбрать админскую группу",
                        request_chat=KeyboardButtonRequestChat(
                            request_id=request_id,
                            chat_is_channel=False,
                            user_administrator_rights=rights(side, True),
                            bot_administrator_rights=rights(side),
                            request_title=True,
                        ),
                    )
                ]
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        return request_id, keyboard

    async def invite(self, actor, rid, side):
        row = await self.get(actor, rid)
        if (
            actor != row["owner_id"]
            or side not in {"work", "admin"}
            or row["state"] != "pending"
        ):
            raise Denied("Приглашение создаёт владелец заявки.")
        token = secrets.token_urlsafe(24)
        async with self.engine.begin() as conn:
            await conn.execute(
                t.selections.insert().values(
                    request_id=secrets.randbelow(2**31 - 1),
                    request=row["id"],
                    actor_id=row[side + "_actor_id"],
                    side=side,
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    expires_at=now() + timedelta(minutes=10),
                )
            )
        return token

    async def accept(self, actor, token):
        async with self.engine.begin() as conn:
            selection = (
                (
                    await conn.execute(
                        select(t.selections)
                        .where(
                            t.selections.c.token_hash
                            == hashlib.sha256(token.encode()).hexdigest()
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if (
                not selection
                or selection["actor_id"] != actor
                or selection["expires_at"] <= now()
                or selection["used_at"]
            ):
                raise Denied(
                    "Приглашение недействительно или предназначено другому человеку."
                )
            await conn.execute(
                t.selections.update()
                .where(t.selections.c.request_id == selection["request_id"])
                .values(used_at=now())
            )
        return await self.selection(actor, selection["request"], selection["side"])

    async def verify(self, chat_id, actor, side):
        try:
            chat = await self.bot.get_chat(chat_id)
            if str(chat.type) not in {"group", "supergroup"}:
                raise Denied("Нужна группа или супергруппа.")
            human = await self.bot.get_chat_member(chat_id, actor)
            bot = await self.bot.get_chat_member(chat_id, self.bot.id)
        except Denied:
            raise
        except Exception as exc:
            raise Denied(
                "Добавьте бота через настройки группы, выдайте права и повторите проверку здесь."
            ) from exc
        if (
            str(human.status) not in {"creator", "administrator"}
            or str(bot.status) != "administrator"
        ):
            raise Denied(
                "Подтверждающий и бот должны быть администраторами выбранной группы."
            )
        required = (
            ["can_delete_messages", "can_restrict_members"] if side == "work" else []
        )
        if str(human.status) != "creator" and not all(
            getattr(human, r, False) for r in required
        ):
            raise Denied("У подтверждающего недостаточно прав.")
        if not all(getattr(bot, r, False) for r in required):
            raise Denied(
                "Боту нужны права удаления сообщений и ограничения участников."
            )
        return chat

    async def share(self, actor, request_id, chat_id):
        async with self.engine.connect() as conn:
            selection = (
                (
                    await conn.execute(
                        select(t.selections).where(
                            t.selections.c.request_id == request_id
                        )
                    )
                )
                .mappings()
                .first()
            )
        if (
            not selection
            or selection["actor_id"] != actor
            or selection["token_hash"]
            or selection["used_at"]
            or selection["expires_at"] <= now()
        ):
            raise Denied("Выбор группы устарел или принадлежит другой заявке.")
        row = await self.get(actor, selection["request"])
        if row["state"] != "pending" or row[selection["side"] + "_actor_id"] != actor:
            raise Denied("Этап заявки недоступен.")
        side = selection["side"]
        await self.verify(chat_id, actor, side)
        async with self.engine.begin() as conn:
            locked = (
                (
                    await conn.execute(
                        select(t.onboarding)
                        .where(t.onboarding.c.id == row["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            selected = (
                (
                    await conn.execute(
                        select(t.selections)
                        .where(t.selections.c.request_id == request_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if (
                locked["state"] != "pending"
                or (not locked["durable"] and locked["expires_at"] <= now())
                or selected["used_at"]
            ):
                raise Conflict("Заявка уже изменилась.")
            if chat_id == locked["admin_chat_id" if side == "work" else "work_chat_id"]:
                raise Conflict("Рабочая и админская группы должны различаться.")
            await conn.execute(
                t.onboarding.update()
                .where(t.onboarding.c.id == row["id"])
                .values(
                    **{
                        side + "_chat_id": chat_id,
                        side + "_confirmed": True,
                        "version": locked["version"] + 1,
                    }
                )
            )
            await conn.execute(
                t.selections.update()
                .where(t.selections.c.request_id == request_id)
                .values(used_at=now())
            )
        return row["id"]

    async def fallback(self, actor, rid, side):
        row = await self.get(actor, rid)
        if side not in {"work", "admin"} or actor != row[side + "_actor_id"]:
            raise Denied("Этот этап предназначен другому участнику.")
        async with self.engine.connect() as conn:
            candidates = (
                (
                    await conn.execute(
                        select(t.candidates)
                        .where(
                            t.candidates.c.inviter_id == actor,
                            t.candidates.c.updated_at > now() - timedelta(days=1),
                        )
                        .order_by(t.candidates.c.updated_at.desc())
                        .limit(20)
                    )
                )
                .mappings()
                .all()
            )
        result = []
        for candidate in candidates:
            try:
                await self.verify(candidate["telegram_chat_id"], actor, side)
                result.append(dict(candidate))
            except Denied:
                continue
        return result

    async def confirm(self, actor, rid, version):
        row = await self.get(actor, rid)
        if (
            not await self.auth.is_owner(actor)
            or row["state"]
            not in ({"submitted"} if row["approval_required"] else {"pending"})
            or row["version"] != version
        ):
            raise Conflict("Подтверждение устарело или недоступно.")
        if not row["work_confirmed"] or not row["admin_confirmed"]:
            raise Conflict("Нужно выбрать и подтвердить обе группы.")
        work = await self.verify(row["work_chat_id"], row["work_actor_id"], "work")
        await self.verify(row["admin_chat_id"], row["admin_actor_id"], "admin")
        manager = await self.bot.get_chat_member(
            row["admin_chat_id"], row["manager_id"]
        )
        if str(manager.status) not in {"creator", "administrator", "member"}:
            raise Denied("Будущий менеджер должен состоять в админской группе.")
        async with self.engine.begin() as conn:
            await conn.execute(text("SELECT pg_advisory_xact_lock(71392145)"))
            locked = (
                (
                    await conn.execute(
                        select(t.onboarding)
                        .where(t.onboarding.c.id == row["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if (
                locked["state"]
                not in ({"submitted"} if locked["approval_required"] else {"pending"})
                or locked["version"] != version
                or (not locked["durable"] and locked["expires_at"] <= now())
                or not await owner(conn, actor)
            ):
                raise Conflict("Заявка изменилась.")
            if (
                await conn.execute(
                    select(t.bindings.c.id).where(
                        t.bindings.c.admin_chat_id == work.id,
                        t.bindings.c.closed_at.is_(None),
                    )
                )
            ).first():
                raise Conflict("Рабочая группа уже используется как админская.")
            admin_work = (
                await conn.execute(
                    select(t.group_settings.c.chat_id)
                    .join(t.aliases, t.aliases.c.chat_id == t.group_settings.c.chat_id)
                    .where(
                        t.aliases.c.telegram_chat_id == row["admin_chat_id"],
                        t.group_settings.c.lifecycle.in_(["connected", "pending"]),
                    )
                )
            ).first()
            if admin_work:
                raise Conflict("Админская группа используется как рабочая.")
            gid = (
                await conn.execute(
                    select(t.aliases.c.chat_id).where(
                        t.aliases.c.telegram_chat_id == work.id
                    )
                )
            ).scalar_one_or_none()
            if gid is not None and row["approval_required"]:
                connected = (
                    await conn.execute(
                        select(t.group_settings.c.lifecycle).where(
                            t.group_settings.c.chat_id == gid
                        )
                    )
                ).scalar_one()
                if connected in {"connected", "pending"}:
                    raise Conflict(
                        "Группа уже подключена. Смену связи выполняет владелец отдельным действием."
                    )
            if gid is None:
                gid = (
                    await conn.execute(
                        t.chats.insert()
                        .values(
                            telegram_chat_id=work.id,
                            title=work.title,
                            type=str(work.type),
                            is_active=False,
                        )
                        .returning(t.chats.c.id)
                    )
                ).scalar_one()
                await conn.execute(
                    t.aliases.insert().values(telegram_chat_id=work.id, chat_id=gid)
                )
                await conn.execute(
                    t.group_settings.insert().values(
                        chat_id=gid,
                        policy=Policy.model_validate(
                            locked["initial_policy"]
                        ).model_dump(),
                    )
                )
            settings = (
                (
                    await conn.execute(
                        select(t.group_settings)
                        .where(t.group_settings.c.chat_id == gid)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            newversion = (
                await conn.execute(
                    select(func.coalesce(func.max(t.bindings.c.version), 0) + 1).where(
                        t.bindings.c.chat_id == gid
                    )
                )
            ).scalar_one()
            previous = (
                (
                    await conn.execute(
                        select(t.bindings).where(
                            t.bindings.c.chat_id == gid,
                            t.bindings.c.closed_at.is_(None),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if previous and settings["lifecycle"] == "connected":
                # Prepare delivery first. Until success the old pair and its roles stay intact.
                await conn.execute(
                    t.onboarding.update()
                    .where(t.onboarding.c.id == row["id"])
                    .values(
                        state="awaiting_delivery",
                        approved_by=actor,
                        approved_version=version,
                    )
                )
                await emit(
                    conn,
                    destination=row["admin_chat_id"],
                    kind="onboarding_transfer",
                    payload={
                        "request": str(row["id"]),
                        "title": work.title,
                        "work_chat_id": work.id,
                        "receipt": secrets.token_urlsafe(9),
                        "next_version": newversion,
                    },
                    key="onboarding:" + str(row["id"]),
                    chat_id=gid,
                    binding_version=previous["version"],
                )
                await audit(
                    conn,
                    gid,
                    actor,
                    "binding.transfer_prepared",
                    request=str(row["id"]),
                )
                return gid
            await conn.execute(
                t.bindings.update()
                .where(t.bindings.c.chat_id == gid, t.bindings.c.closed_at.is_(None))
                .values(closed_at=now())
            )
            await conn.execute(
                t.roles.update()
                .where(t.roles.c.chat_id == gid, t.roles.c.revoked_at.is_(None))
                .values(revoked_at=now())
            )
            await conn.execute(
                t.roles.insert().values(
                    chat_id=gid,
                    actor_id=row["manager_id"],
                    role="manager",
                    granted_by=actor,
                )
            )
            await conn.execute(
                t.bindings.insert().values(
                    chat_id=gid,
                    admin_chat_id=row["admin_chat_id"],
                    version=newversion,
                    confirmed_by=actor,
                )
            )
            await conn.execute(
                t.group_settings.update()
                .where(t.group_settings.c.chat_id == gid)
                .values(
                    lifecycle="pending",
                    mode="observe",
                    version=settings["version"] + 1,
                    updated_at=now(),
                )
            )
            await conn.execute(
                t.actions.update()
                .where(t.actions.c.chat_id == gid, t.actions.c.state == "planned")
                .values(state="cancelled", error="binding_changed")
            )
            await conn.execute(
                t.onboarding.update()
                .where(t.onboarding.c.id == row["id"])
                .values(
                    state="awaiting_delivery",
                    approved_by=actor,
                    approved_version=version,
                )
            )
            await emit(
                conn,
                destination=row["admin_chat_id"],
                kind="onboarding",
                payload={
                    "request": str(row["id"]),
                    "title": work.title,
                    "work_chat_id": work.id,
                    "receipt": secrets.token_urlsafe(9),
                },
                key="onboarding:" + str(row["id"]),
                chat_id=gid,
                binding_version=newversion,
            )
            await audit(
                conn,
                gid,
                actor,
                "binding.prepared",
                request=str(row["id"]),
                version=newversion,
            )
        return gid

    async def acknowledge(self, actor, admin, rid, receipt, message):
        row = await self.get(actor, rid)
        if (
            actor not in {row["owner_id"], row["admin_actor_id"]}
            or row["admin_chat_id"] != admin
        ):
            raise Denied(
                "Доставку подтверждает владелец или подтверждающий админской группы."
            )
        if (
            not message.from_user
            or message.from_user.id != self.bot.id
            or message.chat.id != admin
        ):
            raise Denied(
                "Нужна оригинальная карточка бота в выбранной админской группе."
            )
        await self.verify(row["work_chat_id"], row["work_actor_id"], "work")
        await self.verify(admin, row["admin_actor_id"], "admin")
        member = await self.bot.get_chat_member(admin, actor)
        if str(member.status) not in {"creator", "administrator", "member"}:
            raise Denied("Нужно состоять в админской группе.")
        async with self.engine.begin() as conn:
            if not await owner(conn, row["approved_by"] or row["owner_id"]):
                raise Denied("Полномочия владельца отозваны.")
            delivery = (
                (
                    await conn.execute(
                        select(t.outbox)
                        .where(t.outbox.c.key == "onboarding:" + str(row["id"]))
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if delivery["state"] in {
                "cancelled",
                "failed",
            } or not secrets.compare_digest(
                delivery["payload"].get("receipt", ""), receipt
            ):
                raise Denied("Подтверждение доставки недействительно.")
            if row["state"] not in {"awaiting_delivery", "connected"}:
                raise Conflict("Заявка уже изменилась.")
            await conn.execute(
                t.outbox.update()
                .where(t.outbox.c.id == delivery["id"])
                .values(state="sent", telegram_message_id=message.message_id)
            )
            await self.activate(conn, delivery)
            await audit(
                conn,
                delivery["chat_id"],
                actor,
                "binding.delivery_acknowledged",
                request=str(row["id"]),
                message_id=message.message_id,
            )
        return delivery["chat_id"]

    async def activate(self, conn, outbox):
        row = (
            (
                await conn.execute(
                    select(t.onboarding)
                    .where(t.onboarding.c.id == uuid.UUID(outbox["payload"]["request"]))
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if row["state"] != "awaiting_delivery":
            return
        await conn.execute(
            select(t.group_settings)
            .where(t.group_settings.c.chat_id == outbox["chat_id"])
            .with_for_update()
        )
        binding = (
            await conn.execute(
                select(t.bindings).where(
                    t.bindings.c.chat_id == outbox["chat_id"],
                    t.bindings.c.closed_at.is_(None),
                    t.bindings.c.version == outbox["binding_version"],
                )
            )
        ).first()
        if not binding:
            return
        approving = row["approved_by"] or row["owner_id"]
        if not await owner(conn, approving):
            raise Conflict(
                "Полномочия одобрившего владельца отозваны; подключение не активировано."
            )
        if outbox["kind"] == "onboarding_transfer":
            gid = outbox["chat_id"]
            await conn.execute(
                t.bindings.update()
                .where(t.bindings.c.chat_id == gid, t.bindings.c.closed_at.is_(None))
                .values(closed_at=now())
            )
            await conn.execute(
                t.roles.update()
                .where(t.roles.c.chat_id == gid, t.roles.c.revoked_at.is_(None))
                .values(revoked_at=now())
            )
            await conn.execute(
                t.bindings.insert().values(
                    chat_id=gid,
                    admin_chat_id=row["admin_chat_id"],
                    version=outbox["payload"]["next_version"],
                    confirmed_by=approving,
                )
            )
            await conn.execute(
                t.roles.insert().values(
                    chat_id=gid,
                    actor_id=row["manager_id"],
                    role="manager",
                    granted_by=approving,
                )
            )
            await conn.execute(
                t.group_settings.update()
                .where(t.group_settings.c.chat_id == gid)
                .values(version=t.group_settings.c.version + 1)
            )
            await conn.execute(
                t.actions.update()
                .where(t.actions.c.chat_id == gid, t.actions.c.state == "planned")
                .values(state="cancelled", error="binding_changed")
            )
        await conn.execute(
            t.group_settings.update()
            .where(t.group_settings.c.chat_id == outbox["chat_id"])
            .values(
                lifecycle="connected",
                mode="observe",
                health="healthy",
                health_reason=None,
                updated_at=now(),
            )
        )
        await conn.execute(
            t.onboarding.update()
            .where(t.onboarding.c.id == row["id"])
            .values(state="connected")
        )
        await emit(
            conn,
            destination=row["owner_id"],
            kind="application",
            payload={
                "request": str(row["id"]),
                "text": "✅ Группа подключена. Режим: наблюдение. Откройте панель в выбранной админской группе.",
            },
            key=f"application:{row['id']}:connected",
        )
        await audit(
            conn,
            outbox["chat_id"],
            row["owner_id"],
            "binding.connected",
            request=str(row["id"]),
        )
