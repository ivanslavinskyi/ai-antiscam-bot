from sqlalchemy import select
from app.v2 import tables as t
from app.v2.domain import Denied, GroupScope, Role


async def owner(conn, actor_id: int) -> bool:
    return bool(
        (
            await conn.execute(
                select(t.roles.c.id).where(
                    t.roles.c.actor_id == actor_id,
                    t.roles.c.role == "owner",
                    t.roles.c.chat_id.is_(None),
                    t.roles.c.revoked_at.is_(None),
                )
            )
        ).first()
    )


async def assert_scope(conn, scope: GroupScope, permission: str):
    is_owner = await owner(conn, scope.actor_id)
    if scope.admin_chat_id == scope.actor_id and scope.binding_version == 0:
        if not is_owner or permission not in {"read", "export"}:
            raise Denied("Архив доступен владельцу только для просмотра и экспорта.")
        return
    binding = (
        (
            await conn.execute(
                select(t.bindings).where(
                    t.bindings.c.chat_id == scope.chat_id,
                    t.bindings.c.admin_chat_id == scope.admin_chat_id,
                    t.bindings.c.closed_at.is_(None),
                    t.bindings.c.version == scope.binding_version,
                )
            )
        )
        .mappings()
        .first()
    )
    if not binding:
        raise Denied("Связь с рабочей группой изменилась. Откройте новое меню.")
    role = (
        Role.OWNER
        if is_owner
        else (
            await conn.execute(
                select(t.roles.c.role).where(
                    t.roles.c.actor_id == scope.actor_id,
                    t.roles.c.chat_id == scope.chat_id,
                    t.roles.c.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    )
    if not role:
        raise Denied("Локальная роль отсутствует или отозвана.")
    GroupScope(
        scope.chat_id,
        scope.actor_id,
        scope.admin_chat_id,
        scope.binding_version,
        Role(role),
    ).require(permission)


class Authorization:
    def __init__(self, engine, bot):
        self.engine, self.bot = engine, bot

    async def is_owner(self, actor_id):
        async with self.engine.connect() as conn:
            return await owner(conn, actor_id)

    async def scope(self, actor_id, admin_chat_id, chat_id, permission="read"):
        async with self.engine.connect() as conn:
            binding = (
                (
                    await conn.execute(
                        select(t.bindings).where(
                            t.bindings.c.chat_id == chat_id,
                            t.bindings.c.admin_chat_id == admin_chat_id,
                            t.bindings.c.closed_at.is_(None),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not binding:
                raise Denied("Этот чат не управляет выбранной группой.")
            role = (
                Role.OWNER
                if await owner(conn, actor_id)
                else (
                    await conn.execute(
                        select(t.roles.c.role).where(
                            t.roles.c.chat_id == chat_id,
                            t.roles.c.actor_id == actor_id,
                            t.roles.c.revoked_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
            )
            if not role:
                raise Denied("Нужна локальная роль.")
            scope = GroupScope(
                chat_id, actor_id, admin_chat_id, binding["version"], Role(role)
            )
            scope.require(permission)
        try:
            member = await self.bot.get_chat_member(admin_chat_id, actor_id)
        except Exception as exc:
            raise Denied("Не удалось подтвердить участие в админской группе.") from exc
        if str(member.status) not in {
            "creator",
            "administrator",
            "member",
            "restricted",
        } or (str(member.status) == "restricted" and not member.is_member):
            raise Denied("Вы больше не состоите в админской группе.")
        async with self.engine.connect() as conn:
            await assert_scope(conn, scope, permission)
        return scope

    async def groups(self, actor_id, admin_chat_id):
        async with self.engine.connect() as conn:
            stmt = (
                select(t.chats.c.id, t.chats.c.title)
                .join(t.bindings, t.bindings.c.chat_id == t.chats.c.id)
                .where(
                    t.bindings.c.admin_chat_id == admin_chat_id,
                    t.bindings.c.closed_at.is_(None),
                )
            )
            if not await owner(conn, actor_id):
                stmt = stmt.join(t.roles, t.roles.c.chat_id == t.chats.c.id).where(
                    t.roles.c.actor_id == actor_id, t.roles.c.revoked_at.is_(None)
                )
            rows = [
                dict(r)
                for r in (await conn.execute(stmt.order_by(t.chats.c.id))).mappings()
            ]
        if rows:
            await self.scope(actor_id, admin_chat_id, rows[0]["id"])
        return rows
