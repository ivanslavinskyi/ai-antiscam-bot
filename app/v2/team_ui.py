"""Scoped team directory and invitations; links only register candidates."""

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import select, or_
from sqlalchemy.dialects.postgresql import insert

from app.v2 import tables as t
from app.v2.auth import assert_scope
from app.v2.domain import Conflict, Denied, Role, now
from app.v2.presentation import label


async def remember_contact(conn, admin, user):
    stmt = insert(t.admin_contacts).values(
        admin_chat_id=admin,
        actor_id=user.id,
        name=" ".join(filter(None, [user.first_name, user.last_name])),
        username=user.username,
    )
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["admin_chat_id", "actor_id"],
            set_={
                "name": stmt.excluded.name,
                "username": stmt.excluded.username,
                "updated_at": now(),
            },
        )
    )


class TeamControls:
    async def team(self, scope, candidates=False, query="", offset=0):
        from app.v2.ui import keyboard

        scope.require("roles")
        async with self.engine.connect() as conn:
            await assert_scope(conn, scope, "roles")
            if candidates:
                stmt = select(
                    t.admin_contacts.c.actor_id,
                    t.admin_contacts.c.name,
                    t.admin_contacts.c.username,
                ).where(t.admin_contacts.c.admin_chat_id == scope.admin_chat_id)
                if query:
                    stmt = stmt.where(
                        or_(
                            t.admin_contacts.c.name.icontains(
                                query[:100], autoescape=True
                            ),
                            t.admin_contacts.c.username.icontains(
                                query.lstrip("@")[:100], autoescape=True
                            ),
                        )
                    )
            else:
                stmt = (
                    select(
                        t.roles.c.actor_id,
                        t.roles.c.role,
                        t.admin_contacts.c.name,
                        t.admin_contacts.c.username,
                    )
                    .outerjoin(
                        t.admin_contacts,
                        (t.admin_contacts.c.actor_id == t.roles.c.actor_id)
                        & (t.admin_contacts.c.admin_chat_id == scope.admin_chat_id),
                    )
                    .where(
                        t.roles.c.chat_id == scope.chat_id,
                        t.roles.c.revoked_at.is_(None),
                    )
                )
            people = (
                (
                    await conn.execute(
                        stmt.order_by(
                            t.admin_contacts.c.name, t.admin_contacts.c.actor_id
                        )
                        .offset(max(0, offset))
                        .limit(21)
                    )
                )
                .mappings()
                .all()
            )
        rows = []
        for person in people[:20]:
            name = person["name"] or f"Участник {person['actor_id']}"
            name += f" · @{person['username']}" if person["username"] else ""
            if person.get("role"):
                name += " · " + label(person["role"])
            rows.append(
                [(name[:90], f"v:teamuser:{scope.chat_id}:{person['actor_id']}")]
            )
        if len(people) > 20 and not query:
            rows.append(
                [
                    (
                        "Далее",
                        f"v:teamlist:{scope.chat_id}:{int(candidates)}:{offset + 20}",
                    )
                ]
            )
        if candidates:
            rows += [
                [
                    ("Найти по имени", f"v:teamsearch:{scope.chat_id}"),
                    ("Нужного человека нет", f"v:teaminvite:{scope.chat_id}"),
                ]
            ]
        else:
            rows += [[("Добавить в команду", f"v:teamlist:{scope.chat_id}:1:0")]]
        rows.append([("Меню", f"v:menu:{scope.chat_id}")])
        title = (await self.policy_row(scope))["title"]
        await self.send(
            scope.admin_chat_id,
            "🧑‍💼 Команда · "
            + title
            + (
                "\nВыберите человека. Известный контакт не получает прав автоматически."
                if candidates
                else "\nРоли действуют только в этой рабочей группе."
            ),
            keyboard(rows),
        )

    async def team_user(self, scope, actor):
        from app.v2.ui import keyboard

        scope.require("roles")
        async with self.engine.connect() as conn:
            await assert_scope(conn, scope, "roles")
            contact = (
                (
                    await conn.execute(
                        select(t.admin_contacts).where(
                            t.admin_contacts.c.admin_chat_id == scope.admin_chat_id,
                            t.admin_contacts.c.actor_id == actor,
                        )
                    )
                )
                .mappings()
                .first()
            )
            existing = (
                (
                    await conn.execute(
                        select(t.roles).where(
                            t.roles.c.chat_id == scope.chat_id,
                            t.roles.c.actor_id == actor,
                            t.roles.c.revoked_at.is_(None),
                        )
                    )
                )
                .mappings()
                .first()
            )
        if not contact and not existing:
            raise Denied(
                "Кандидат не найден в этой админской группе. Используйте приглашение."
            )
        if existing and existing["role"] == "manager" and scope.role != Role.OWNER:
            raise Denied("Управляющего меняет владелец бота.")
        title = (await self.policy_row(scope))["title"]
        name = (contact["name"] if contact else None) or f"Участник {actor}"
        buttons = []
        # Tokens are bound to the issuing actor, current role and binding.
        for role, caption in (
            ("moderator", "Назначить модератором"),
            ("observer", "Назначить наблюдателем"),
        ):
            if existing and existing["role"] == role:
                continue
            token = await self.operator.issue(
                scope,
                "role",
                {
                    "actor_id": actor,
                    "role": role,
                    "expected_role_id": str(existing["id"]) if existing else None,
                },
            )
            buttons.append([(caption, "v:confirm:" + token)])
        if existing:
            buttons.append(
                [("Убрать из команды", f"v:teamremove:{scope.chat_id}:{actor}")]
            )
        buttons.append([("Назад", f"v:teamlist:{scope.chat_id}:0:0")])
        await self.send(
            scope.admin_chat_id,
            f"👤 {name}\n👥 {title}\nРоль: {label(existing['role']) if existing else 'не назначена'}\n\nМодератор — решения, списки, бан и мут.\nНаблюдатель — только просмотр.\nКнопка роли подтверждает назначение.",
            keyboard(buttons),
        )

    async def team_invite(self, scope):
        from app.v2.ui import keyboard

        scope.require("roles")
        token = secrets.token_urlsafe(24)
        async with self.engine.begin() as conn:
            await assert_scope(conn, scope, "roles")
            await conn.execute(
                t.team_invitations.insert().values(
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    chat_id=scope.chat_id,
                    admin_chat_id=scope.admin_chat_id,
                    binding_version=scope.binding_version,
                    inviter_id=scope.actor_id,
                    expires_at=now() + timedelta(days=1),
                )
            )
        me = await self.bot.get_me()
        await self.send(
            scope.admin_chat_id,
            f"🔗 Передайте кандидату ссылку (действует сутки):\nhttps://t.me/{me.username}?start=team_{token}\n\nЧеловек должен состоять в этой админской группе. Ссылка не выдаёт роль — после входа выберите её кнопкой.",
            keyboard([[("Назад", f"v:teamlist:{scope.chat_id}:0:0")]]),
        )

    async def accept_team_invite(self, user, token):
        from app.v2.ui import keyboard

        async with self.engine.connect() as conn:
            invite = (
                (
                    await conn.execute(
                        select(t.team_invitations).where(
                            t.team_invitations.c.token_hash
                            == hashlib.sha256(token.encode()).hexdigest()
                        )
                    )
                )
                .mappings()
                .first()
            )
        if (
            not invite
            or invite["expires_at"] <= now()
            or invite["candidate_id"] not in {None, user.id}
        ):
            raise Conflict(
                "Приглашение недоступно или уже использовано другим человеком."
            )
        scope = await self.auth.scope(
            invite["inviter_id"], invite["admin_chat_id"], invite["chat_id"], "roles"
        )
        if scope.binding_version != invite["binding_version"]:
            raise Conflict("Админская группа изменилась. Запросите новое приглашение.")
        member = await self.bot.get_chat_member(scope.admin_chat_id, user.id)
        if str(member.status) not in {"creator", "administrator", "member"}:
            raise Denied(
                "Сначала попросите добавить вас в админскую группу, затем откройте ссылку снова."
            )
        async with self.engine.begin() as conn:
            await assert_scope(conn, scope, "roles")
            current = (
                (
                    await conn.execute(
                        select(t.team_invitations)
                        .where(t.team_invitations.c.id == invite["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if (
                current["candidate_id"] not in {None, user.id}
                or current["expires_at"] <= now()
            ):
                raise Conflict("Приглашение изменилось.")
            repeat = current["candidate_id"] == user.id
            await remember_contact(conn, scope.admin_chat_id, user)
            await conn.execute(
                t.team_invitations.update()
                .where(t.team_invitations.c.id == invite["id"])
                .values(candidate_id=user.id)
            )
        if not repeat:
            await self.send(
                scope.admin_chat_id,
                "👤 Кандидат в команду: "
                + user.full_name
                + "\nВыберите роль после проверки имени.",
                keyboard(
                    [[("Открыть кандидата", f"v:teamuser:{scope.chat_id}:{user.id}")]]
                ),
            )
        await self.send(
            user.id,
            "✅ Вы зарегистрированы как кандидат. Управляющий назначит роль отдельно.",
        )

    async def team_callback(self, scope, parts):
        cmd = parts[1]
        if cmd == "teamlist":
            await self.team(scope, bool(int(parts[3])), offset=int(parts[4]))
        elif cmd == "teamuser":
            await self.team_user(scope, int(parts[3]))
        elif cmd == "teaminvite":
            await self.team_invite(scope)
        elif cmd == "teamsearch":
            scope.require("roles")
            await self.save_session(
                scope.actor_id, scope.admin_chat_id, scope.chat_id, form="teamsearch"
            )
            await self.send(
                scope.admin_chat_id,
                "🔍 Напишите имя или username кандидата. Для выхода — /as.",
            )
        elif cmd == "teamremove":
            async with self.engine.connect() as conn:
                existing = (
                    (
                        await conn.execute(
                            select(t.roles).where(
                                t.roles.c.chat_id == scope.chat_id,
                                t.roles.c.actor_id == int(parts[3]),
                                t.roles.c.revoked_at.is_(None),
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
            if not existing:
                raise Conflict("Доступ уже отозван.")
            await self.confirmation(
                scope,
                "role",
                {
                    "actor_id": int(parts[3]),
                    "role": "remove",
                    "expected_role_id": str(existing["id"]),
                },
                description="Отозвать локальный доступ. Человек останется в Telegram-чате, история решений сохранится.",
            )
        else:
            return False
        return True
