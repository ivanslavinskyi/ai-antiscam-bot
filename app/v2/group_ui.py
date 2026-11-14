"""Owner lifecycle controls. Private confirmations retain real group authorization."""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.v2 import tables as t
from app.v2.domain import Denied, Conflict, now
from app.v2.auth import assert_scope
from app.v2.queue import emit
from app.v2.presentation import label


class GroupControls:
    async def lifecycle_callback(self, scope, parts):
        from app.v2.ui import keyboard

        if parts[1] not in {"life", "lreq"}:
            return False
        scope.require("settings")
        kinds = {
            "rebind": "Сменить админскую группу",
            "manager": "Передать управление",
            "disconnect": "Отключить группу",
        }
        if parts[1] == "life":
            row = await self.policy_row(scope)
            await self.send(
                scope.admin_chat_id,
                f"🔗 {row['title']}\nПауза доступна вам. Изменение подключения выполняет владелец бота после вашего запроса. История и списки сохраняются.",
                keyboard(
                    [[("Пауза / возобновление", f"v:modes:{scope.chat_id}")]]
                    + [
                        [(caption, f"v:lreq:{scope.chat_id}:{kind}")]
                        for kind, caption in kinds.items()
                    ]
                    + [[("Меню", "v:home")]]
                ),
            )
            return True
        kind = parts[3]
        if kind not in kinds:
            raise Conflict("Неизвестный тип запроса.")
        async with self.engine.begin() as conn:
            await assert_scope(conn, scope, "settings")
            request_id = (
                await conn.execute(
                    insert(t.group_requests)
                    .values(
                        chat_id=scope.chat_id,
                        actor_id=scope.actor_id,
                        kind=kind,
                    )
                    .on_conflict_do_nothing()
                    .returning(t.group_requests.c.id)
                )
            ).scalar_one_or_none()
            if request_id:
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
                title = (
                    await conn.execute(
                        select(t.chats.c.title).where(t.chats.c.id == scope.chat_id)
                    )
                ).scalar_one()
                for owner in owners:
                    await emit(
                        conn,
                        destination=owner,
                        kind="group_request",
                        payload={
                            "request": str(request_id),
                            "text": f"📨 Запрос управляющего\n👥 {title}\n{kinds[kind]}",
                            "owner_only": True,
                        },
                        key=f"grouprequest:{request_id}:{owner}",
                    )
        await self.send(
            scope.admin_chat_id,
            "📨 Запрос сохранён и будет доставлен владельцу. Текущая связка продолжает работать."
            if request_id
            else "📨 Такой запрос уже ожидает владельца.",
            keyboard([[("Меню", "v:home")]]),
        )
        return True

    async def owner_requests(self, actor, parts):
        from app.v2.ui import keyboard

        cmd = parts[1]
        async with self.engine.begin() as conn:
            if not await self.auth.is_owner(actor):
                raise Denied("Доступно владельцу бота.")
            if cmd == "groupresolve":
                state = parts[3]
                if state not in {"closed", "rejected"}:
                    raise Conflict("Неизвестный результат.")
                await conn.execute(
                    t.group_requests.update()
                    .where(
                        t.group_requests.c.id == uuid.UUID(parts[2]),
                        t.group_requests.c.state == "open",
                    )
                    .values(state=state, resolved_by=actor, resolved_at=now())
                )
            stmt = select(t.group_requests, t.chats.c.title).join(
                t.chats, t.chats.c.id == t.group_requests.c.chat_id
            )
            if cmd == "grouprequest":
                stmt = stmt.where(t.group_requests.c.id == uuid.UUID(parts[2]))
            else:
                offset = max(0, int(parts[2])) if cmd == "grouprequests" else 0
                stmt = (
                    stmt.where(t.group_requests.c.state == "open")
                    .order_by(t.group_requests.c.created_at)
                    .offset(offset)
                    .limit(21)
                )
            requests = (await conn.execute(stmt)).mappings().all()
        if cmd == "grouprequest" and requests:
            row = requests[0]
            caption = {
                "rebind": "Сменить админскую группу",
                "manager": "Передать управление",
                "disconnect": "Отключить группу",
            }[row["kind"]]
            buttons = [[("Открыть управление группой", f"v:manage:{row['chat_id']}")]]
            if row["state"] == "open":
                buttons += [
                    [
                        (
                            "Отметить выполненным",
                            f"v:groupresolve:{row['id'].hex}:closed",
                        )
                    ],
                    [("Отклонить запрос", f"v:groupresolve:{row['id'].hex}:rejected")],
                ]
            text = f"📨 {caption}\n👥 {row['title']}\nЗапрос от {row['actor_id']}\nСостояние: {label(row['state'])}\nЗакрытие запроса само по себе не меняет подключение группы."
        else:
            text = "📨 Запросы управляющих" + (
                "\nОткрытых запросов нет." if not requests else ""
            )
            buttons = [
                [(r["title"], f"v:grouprequest:{r['id'].hex}")] for r in requests[:20]
            ]
            if len(requests) > 20:
                buttons.append([("Далее", f"v:grouprequests:{offset + 20}")])
        buttons.append([("Меню", "v:system:0")])
        await self.send(actor, text, keyboard(buttons))

    async def owner_group(self, actor, gid):
        from app.v2.ui import keyboard

        if not await self.auth.is_owner(actor):
            raise Denied("Управление подключением доступно владельцу.")
        async with self.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(t.group_settings, t.chats.c.title)
                        .join(t.chats, t.chats.c.id == t.group_settings.c.chat_id)
                        .where(t.group_settings.c.chat_id == gid)
                    )
                )
                .mappings()
                .one()
            )
            binding = (
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
            managers = (
                (
                    await conn.execute(
                        select(t.roles.c.actor_id).where(
                            t.roles.c.chat_id == gid,
                            t.roles.c.role == "manager",
                            t.roles.c.revoked_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
        rows = [[("История", f"v:page:{gid}:history")]]
        if row["lifecycle"] == "connected" and binding:
            rows += [
                [("Открыть админскую панель", f"v:groupopen:{gid}")],
                [("Сменить админскую группу", f"v:rebind:{gid}")],
                [("Сменить управляющего", f"v:managerpick:{gid}")],
                [
                    (
                        "Возобновить наблюдение"
                        if row["mode"] == "paused"
                        else "Поставить на паузу",
                        f"v:ownerpause:{gid}",
                    )
                ],
                [("Отключить группу", f"v:archive:{gid}")],
            ]
        else:
            rows += [[("Подключить снова", f"v:rebind:{gid}")]]
        rows.append([("Мои группы", "v:mygroups:0")])
        text = f"🔗 {row['title']}\nРежим: {label(row['mode'])} · {label(row['lifecycle'])}\nУправляющий: {', '.join(map(str, managers)) or 'не назначен'}\nИстория, списки и ограничения сохраняются."
        await self.send(actor, text, keyboard(rows))

    async def owner_scope(self, actor, gid):
        if not await self.auth.is_owner(actor):
            raise Denied("Нужна действующая роль владельца.")
        async with self.engine.connect() as conn:
            admin = (
                await conn.execute(
                    select(t.bindings.c.admin_chat_id).where(
                        t.bindings.c.chat_id == gid, t.bindings.c.closed_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
        if admin is None:
            raise Conflict("Группа отключена. Сначала пройдите повторное подключение.")
        return await self.auth.scope(actor, admin, gid, "system")

    async def owner_confirmation(self, actor, scope, kind, payload, description):
        from app.v2.ui import keyboard

        token = await self.operator.issue(scope, kind, payload)
        title = (await self.policy_row(scope))["title"]
        await self.send(
            actor,
            f"👥 {title}\n{description}",
            keyboard(
                [
                    [("Подтвердить", "v:oc:" + token)],
                    [("Отмена", f"v:manage:{scope.chat_id}")],
                ]
            ),
        )

    async def group_callback(self, actor, admin, parts):
        from app.v2.ui import keyboard

        cmd = parts[1]
        if cmd not in {
            "manage",
            "oc",
            "archive",
            "ownerpause",
            "rebind",
            "archives",
            "managerpick",
            "managerchange",
            "grouprequests",
            "grouprequest",
            "groupresolve",
        }:
            return False
        if actor != admin or not await self.auth.is_owner(actor):
            raise Denied("Управление подключениями доступно в личке владельца.")
        if cmd in {"grouprequests", "grouprequest", "groupresolve"}:
            await self.owner_requests(actor, parts)
            return True
        if cmd == "oc":
            async with self.engine.connect() as conn:
                op = (
                    (
                        await conn.execute(
                            select(t.operations).where(
                                t.operations.c.token == parts[2],
                                t.operations.c.actor_id == actor,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
            if not op or op["kind"] not in {"mode", "disconnect", "role"}:
                raise Denied("Подтверждение недоступно.")
            result = await self.operator.confirm(actor, op["admin_chat_id"], parts[2])
            await self.send(
                actor,
                "✅ " + result["message"],
                keyboard([[("Открыть группу", f"v:manage:{op['chat_id']}")]]),
            )
            return True
        if cmd == "archives":
            async with self.engine.connect() as conn:
                groups = (
                    (
                        await conn.execute(
                            select(t.chats.c.id, t.chats.c.title)
                            .join(
                                t.group_settings,
                                t.group_settings.c.chat_id == t.chats.c.id,
                            )
                            .where(
                                t.group_settings.c.lifecycle.in_(
                                    ["archived", "disconnected"]
                                )
                            )
                            .order_by(t.chats.c.id)
                            .offset(int(parts[2]))
                            .limit(21)
                        )
                    )
                    .mappings()
                    .all()
                )
            rows = [[(g["title"], f"v:manage:{g['id']}")] for g in groups[:20]]
            if len(groups) > 20:
                rows.append([("Далее", f"v:archives:{int(parts[2]) + 20}")])
            rows.append([("Меню", "v:system:0")])
            await self.send(
                actor, "📦 Архив. История и списки сохранены.", keyboard(rows)
            )
            return True
        gid = int(parts[2])
        if cmd == "manage":
            await self.owner_group(actor, gid)
        elif cmd == "rebind":
            async with self.engine.connect() as conn:
                work = (
                    await conn.execute(
                        select(t.chats.c.telegram_chat_id).where(t.chats.c.id == gid)
                    )
                ).scalar_one()
                manager = (
                    await conn.execute(
                        select(t.roles.c.actor_id)
                        .where(
                            t.roles.c.chat_id == gid,
                            t.roles.c.role == "manager",
                            t.roles.c.revoked_at.is_(None),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
            rid = await self.onboarding.create(actor, manager=manager or actor)
            selection, _ = await self.onboarding.selection(actor, rid, "work")
            try:
                await self.onboarding.share(actor, selection, work)
            except Denied:
                await self.send(
                    actor,
                    "🔗 Подтвердите рабочую группу в мастере. Если прав недостаточно, потребуется её администратор.",
                )
            await self.show_onboarding(actor, rid)
        elif cmd in {"archive", "ownerpause"}:
            scope = await self.owner_scope(actor, gid)
            row = await self.policy_row(scope)
            if cmd == "archive":
                await self.owner_confirmation(
                    actor,
                    scope,
                    "disconnect",
                    {},
                    "📦 Отключить и сохранить в архиве? Проверки и управление через старые карточки прекратятся. История, списки и баны сохранятся.",
                )
            elif row["mode"] == "paused":
                await self.execute_quick(
                    scope,
                    "mode",
                    {"mode": "observe", "expected_policy_version": row["version"]},
                )
                await self.owner_group(actor, gid)
            else:
                await self.owner_confirmation(
                    actor,
                    scope,
                    "mode",
                    {"mode": "paused"},
                    "⏸ Остановить новые проверки и автоматические действия? Уже отправленные действия могут завершиться; история и ограничения сохраняются.",
                )
        elif cmd == "managerpick":
            scope = await self.owner_scope(actor, gid)
            async with self.engine.connect() as conn:
                contacts = (
                    (
                        await conn.execute(
                            select(t.admin_contacts)
                            .where(
                                t.admin_contacts.c.admin_chat_id == scope.admin_chat_id
                            )
                            .order_by(t.admin_contacts.c.name)
                            .limit(20)
                        )
                    )
                    .mappings()
                    .all()
                )
            rows = [
                [(c["name"], f"v:managerchange:{gid}:{c['actor_id']}:ask")]
                for c in contacts
            ]
            rows += [
                [("Назначить меня", f"v:managerchange:{gid}:{actor}:ask")],
                [("Назад", f"v:manage:{gid}")],
            ]
            await self.send(
                actor,
                "🧑‍💼 Выберите нового управляющего. Для нового человека используйте приглашение команды в админке.",
                keyboard(rows),
            )
        elif cmd == "managerchange":
            scope = await self.owner_scope(actor, gid)
            target = int(parts[3])
            async with self.engine.connect() as conn:
                existing = (
                    await conn.execute(
                        select(t.roles.c.id).where(
                            t.roles.c.chat_id == gid,
                            t.roles.c.actor_id == target,
                            t.roles.c.revoked_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                managers = (
                    (
                        await conn.execute(
                            select(t.roles.c.id).where(
                                t.roles.c.chat_id == gid,
                                t.roles.c.role == "manager",
                                t.roles.c.revoked_at.is_(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            payload = {
                "actor_id": target,
                "role": "manager",
                "replace_managers": True,
                "expected_role_id": str(existing) if existing else None,
                "expected_managers": sorted(str(i) for i in managers),
            }
            rows = []
            for keep, caption in (
                (False, "Передать управление"),
                (True, "Передать, оставив прежнего модератором"),
            ):
                token = await self.operator.issue(
                    scope, "role", {**payload, "keep_previous": keep}
                )
                rows.append([(caption, "v:oc:" + token)])
            rows.append([("Отмена", f"v:manage:{gid}")])
            await self.send(
                actor,
                f"🧑‍💼 Передать управление участнику {target}?\n👥 {(await self.policy_row(scope))['title']}\nПо умолчанию прежний локальный доступ отзывается. Системная роль владельца не меняется.",
                keyboard(rows),
            )
        return True
