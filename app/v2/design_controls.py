"""Approved compact controls; all writes still use Operator's transaction fences."""

import uuid

from sqlalchemy import select

from app.v2 import tables as t
from app.v2.auth import assert_scope
from app.v2.domain import Conflict, Denied, Policy, Role
from app.v2.presentation import label
from app.v2.team_ui import TeamControls
from app.v2.feed_ui import FeedControls
from app.v2.group_ui import GroupControls


PRESETS = {
    "auto_whitelist_threshold": [("3", 3), ("5", 5), ("10", 10)],
    "warn_at": [(str(v), v) for v in (1, 2, 3)],
    "mute_at": [(str(v), v) for v in (2, 3, 4, 5)],
    "ban_at": [(str(v), v) for v in (3, 5, 7, 10)],
    "mute_seconds": [("1 час", 3600), ("24 часа", 86400), ("7 дней", 604800)],
    "notice_ttl_seconds": [("1 минута", 60), ("5 минут", 300), ("Не удалять", 0)],
    "max_action_age_seconds": [("5 минут", 300), ("15 минут", 900), ("1 час", 3600)],
    "confidence_threshold": [("80%", 0.8), ("90%", 0.9), ("95%", 0.95)],
    "daily_summary_time": [
        ("09:00", "09:00"),
        ("18:00", "18:00"),
        ("21:00", "21:00"),
        ("Выключена", None),
    ],
    "timezone": [("Цюрих", "Europe/Zurich"), ("Киев", "Europe/Kyiv"), ("UTC", "UTC")],
}

DESCRIPTIONS = {
    "trust_add": "🤍 Выдать доверие только в этой группе. Ограничения и страйки сохраняются.",
    "trust_remove": "🔍 Снять доверие и начать новый цикл проверок в этой группе.",
    "ban": "🔨 Забанить участника только в этой группе.",
    "unban": "🔓 Снять бан. Это не возвращает человека в чат и не снимает блэклист.",
    "mute": "🔇 Запретить писать на 24 часа только в этой группе.",
    "unmute": "🔊 Снять допустимое ограничение этого бота с учётом общих прав группы.",
    "deny_add": "⛔ Блэклист: удалять новые сообщения в боевом режиме. Бан и вайтлист не меняются.",
    "deny_remove": "⛔ Убрать блэклист. Бан и мут сохраняются.",
    "reset_strikes": "↩️ Обнулить страйки. История, списки, бан и мут сохраняются.",
}


class DesignControls(TeamControls, FeedControls, GroupControls):
    async def choose(self, actor, admin, offset=0):
        return await self.admin_home(actor, admin)

    async def system(self, actor, offset=0):
        from app.v2.ui import keyboard

        is_owner = await self.auth.is_owner(actor)
        await self.save_session(actor, actor)
        requests = await self.onboarding.applications(actor)
        pending = sum(r["state"] == "submitted" for r in requests)
        heading = "🛡 Панель владельца" if is_owner else "👤 Моя панель"
        rows = [
            [
                ("Мои группы", "v:mygroups:0"),
                (f"Заявки · {pending}" if is_owner else "Мои заявки", "v:apps:0"),
            ],
            [("Подключить группу", "v:new")],
        ]
        if is_owner:
            rows += [
                [("👥 Подключение с другими людьми", "v:newpeople")],
                [("Запросы управляющих", "v:grouprequests:0")],
                [("Состояние бота", "v:service:0")],
                [("Бюджеты и расходы", "v:budgets:0")],
            ]
        rows.append([("Помощь", "v:privatehelp")])
        await self.send(
            actor,
            heading
            + "\n\nПодключение проходит здесь. Модерация — в связанной админской группе.",
            keyboard(rows),
        )

    async def show_onboarding(self, actor, rid):
        from app.v2.ui import keyboard

        row = await self.onboarding.get(actor, rid)
        rid = row["id"]
        await self.save_session(actor, actor, onboarding=str(rid))
        names = {}
        for side in ("work", "admin"):
            names[side] = "не выбрана"
            if row[side + "_chat_id"]:
                try:
                    chat = await self.bot.get_chat(row[side + "_chat_id"])
                    names[side] = f"{chat.title or 'Группа'} ({chat.id})"
                except Exception:
                    names[side] = "название недоступно; нажмите обновить"
        async with self.engine.connect() as conn:
            manager_name = (
                await conn.execute(
                    select(t.admin_contacts.c.name).where(
                        t.admin_contacts.c.actor_id == row["manager_id"],
                        t.admin_contacts.c.admin_chat_id == row["admin_chat_id"],
                    )
                )
            ).scalar_one_or_none()
        manager_name = (
            "вы"
            if actor == row["manager_id"]
            else manager_name or str(row["manager_id"])
        )
        text = (
            f"🔗 Подключение · {'Черновик' if row['state'] == 'pending' else label(row['state'])}\n👥 Рабочая: {names['work']}\n"
            f"🧑‍💼 Админская: {names['admin']}\nУправляющий: {manager_name}\n"
            "👁 Старт: наблюдение\n"
            "Команды в рабочей группе не нужны."
        )
        if row["review_note"]:
            text += "\n📝 " + row["review_note"]
        rows = []
        if row["state"] == "pending":
            for side, caption in (("work", "Рабочая"), ("admin", "Админская")):
                if row[side + "_actor_id"] == actor:
                    rows.append(
                        [
                            (caption + ": выбрать группу", f"v:pick:{rid.hex}:{side}"),
                            ("Резервный выбор", f"v:fallback:{rid.hex}:{side}"),
                        ]
                    )
                elif row["owner_id"] == actor:
                    rows.append(
                        [
                            (
                                f"🔗 {caption}: передать выбор",
                                f"v:invite:{rid.hex}:{side}",
                            )
                        ]
                    )
            if row["owner_id"] == actor:
                if await self.auth.is_owner(actor) and row["admin_chat_id"]:
                    rows.append(
                        [
                            (
                                "Выбрать управляющего",
                                f"v:apmanager:{rid.hex}:{row['version']}",
                            )
                        ]
                    )
                rows.append(
                    [
                        (
                            "Начальные настройки",
                            f"v:apsettings:{rid.hex}:{row['version']}",
                        )
                    ]
                )
                if row["work_confirmed"] and row["admin_confirmed"]:
                    rows.append(
                        [
                            (
                                "Отправить заявку"
                                if row["approval_required"]
                                else "Подключить",
                                f"v:{'submit' if row['approval_required'] else 'connect'}:{rid.hex}:{row['version']}",
                            )
                        ]
                    )
        elif row["state"] == "submitted":
            text += "\n📨 Ожидает решения владельца. Модерация не включена."
            if await self.auth.is_owner(actor):
                rows += [
                    [("Одобрить", f"v:appapprove:{rid.hex}:{row['version']}")],
                    [
                        (
                            "Попросить исправить",
                            f"v:appnote:{rid.hex}:{row['version']}:revise",
                        ),
                        ("Отклонить", f"v:appnote:{rid.hex}:{row['version']}:reject"),
                    ],
                ]
        if row["owner_id"] == actor and row["state"] in {"pending", "submitted"}:
            rows.append(
                [("Отозвать заявку", f"v:appwithdraw:{rid.hex}:{row['version']}")]
            )
        rows += [[("Обновить", f"v:app:{rid.hex}"), ("Меню", "v:system:0")]]
        await self.send(actor, text, keyboard(rows))

    async def design_private_callback(self, actor, admin, parts):
        from app.v2.ui import keyboard

        cmd = parts[1]
        commands = {
            "apps",
            "app",
            "submit",
            "appapprove",
            "appaccept",
            "appnote",
            "appwithdraw",
            "apsettings",
            "apvalue",
            "mygroups",
            "groupopen",
            "service",
            "privatehelp",
            "apmanager",
            "am",
        }
        if cmd not in commands:
            return False
        if actor != admin:
            raise Denied("Откройте личный чат с ботом.")
        if cmd in {"apmanager", "am"}:
            row = await self.onboarding.get(actor, parts[2])
            if row["owner_id"] != actor or not await self.auth.is_owner(actor):
                raise Denied("Выбор доступен владельцу в своём мастере.")
            if cmd == "am":
                await self.onboarding.set_manager(
                    actor, row["id"], int(parts[3], 16), int(parts[4], 16)
                )
                await self.show_onboarding(actor, row["id"])
            else:
                async with self.engine.connect() as conn:
                    contacts = (
                        (
                            await conn.execute(
                                select(t.admin_contacts)
                                .where(
                                    t.admin_contacts.c.admin_chat_id
                                    == row["admin_chat_id"],
                                )
                                .order_by(t.admin_contacts.c.name)
                                .limit(30)
                            )
                        )
                        .mappings()
                        .all()
                    )
                buttons = [
                    [
                        (
                            c["name"],
                            f"v:am:{row['id'].hex}:{row['version']:x}:{c['actor_id']:x}",
                        )
                    ]
                    for c in contacts
                ]
                buttons += [
                    [
                        (
                            "Назначить меня",
                            f"v:am:{row['id'].hex}:{row['version']:x}:{actor:x}",
                        )
                    ],
                    [("Назад", f"v:app:{row['id'].hex}")],
                ]
                await self.send(
                    actor,
                    "🧑‍💼 Выберите управляющего из участников админки, известных боту. Если нужного человека нет, пусть отправит /as в выбранной админке, затем откройте список снова.",
                    keyboard(buttons),
                )
        elif cmd == "privatehelp":
            await self.send(
                actor,
                "ℹ️ Выберите две группы: рабочую для проверки и админскую для команды. Подайте заявку владельцу; после одобрения включится наблюдение. Команды и ID в рабочей группе не нужны.",
                keyboard([[("Подключить группу", "v:new"), ("Меню", "v:system:0")]]),
            )
        elif cmd == "service":
            if not await self.auth.is_owner(actor):
                raise Denied("Раздел доступен владельцу.")
            await self.system_status(actor)
        elif cmd == "mygroups":
            async with self.engine.connect() as conn:
                query = (
                    select(t.chats.c.id, t.chats.c.title, t.bindings.c.admin_chat_id)
                    .join(t.bindings, t.bindings.c.chat_id == t.chats.c.id)
                    .join(t.group_settings, t.group_settings.c.chat_id == t.chats.c.id)
                    .where(
                        t.bindings.c.closed_at.is_(None),
                        t.group_settings.c.lifecycle == "connected",
                    )
                )
                if not await self.auth.is_owner(actor):
                    query = query.join(
                        t.roles, t.roles.c.chat_id == t.chats.c.id
                    ).where(t.roles.c.actor_id == actor, t.roles.c.revoked_at.is_(None))
                groups = (
                    (
                        await conn.execute(
                            query.order_by(t.chats.c.id).offset(int(parts[2])).limit(21)
                        )
                    )
                    .mappings()
                    .all()
                )
            rows = []
            for group in groups[:20]:
                if not await self.auth.is_owner(actor):
                    try:
                        await self.auth.scope(
                            actor, group["admin_chat_id"], group["id"]
                        )
                    except Denied:
                        continue
                rows.append(
                    [
                        (
                            group["title"],
                            f"v:{'manage' if await self.auth.is_owner(actor) else 'groupopen'}:{group['id']}",
                        )
                    ]
                )
            if len(groups) > 20:
                rows.append([("Далее", f"v:mygroups:{int(parts[2]) + 20}")])
            if await self.auth.is_owner(actor):
                rows.append([("Архив", "v:archives:0")])
            rows.append([("Меню", "v:system:0")])
            await self.send(
                actor,
                "👥 Мои группы"
                if len(rows) > 1
                else "👥 Подключённых доступных групп пока нет.",
                keyboard(rows),
            )
        elif cmd == "groupopen":
            gid = int(parts[2])
            async with self.engine.connect() as conn:
                binding = (
                    await conn.execute(
                        select(t.bindings.c.admin_chat_id).where(
                            t.bindings.c.chat_id == gid,
                            t.bindings.c.closed_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
            if binding is None:
                raise Conflict("Группа отключена. История доступна владельцу в архиве.")
            scope = await self.auth.scope(actor, binding, gid)
            await self.menu(scope)
            await self.send(
                actor,
                "✅ Панель открыта в админской группе.",
                keyboard([[("Мои группы", "v:mygroups:0")]]),
            )
        elif cmd == "apps":
            requests = await self.onboarding.applications(actor, int(parts[2]))
            rows = [
                [
                    (
                        f"{label(r['state'])} · {r['created_at'].strftime('%d.%m %H:%M')}",
                        f"v:app:{r['id'].hex}",
                    )
                ]
                for r in requests
            ]
            if len(requests) == 20:
                rows.append([("Далее", f"v:apps:{int(parts[2]) + 20}")])
            rows.append([("Меню", "v:system:0")])
            await self.send(
                actor,
                "📨 Заявки" if requests else "📨 Заявок пока нет.",
                keyboard(rows),
            )
        elif cmd == "app":
            await self.show_onboarding(actor, parts[2])
        elif cmd == "submit":
            await self.onboarding.submit(actor, parts[2], int(parts[3]))
            await self.show_onboarding(actor, parts[2])
        elif cmd == "appapprove":
            if not await self.auth.is_owner(actor):
                raise Denied("Одобрение доступно только владельцу.")
            row = await self.onboarding.get(actor, parts[2])
            if row["version"] != int(parts[3]) or row["state"] != "submitted":
                raise Conflict("Заявка изменилась.")
            await self.send(
                actor,
                "✅ Подключить выбранную пару из заявки выше? Права будут проверены повторно; старт — наблюдение.",
                keyboard(
                    [
                        [("Подключить", f"v:appaccept:{parts[2]}:{parts[3]}")],
                        [("Отмена", f"v:app:{parts[2]}")],
                    ]
                ),
            )
        elif cmd == "appaccept":
            await self.onboarding.confirm(actor, parts[2], int(parts[3]))
            await self.show_onboarding(actor, parts[2])
        elif cmd == "appnote":
            if not await self.auth.is_owner(actor):
                raise Denied("Решение принимает владелец.")
            await self.save_session(
                actor,
                actor,
                form="application_note",
                request=parts[2],
                version=int(parts[3]),
                decision=parts[4],
            )
            await self.send(
                actor,
                "📝 Напишите короткое пояснение для заявителя.",
                keyboard([[("Отмена", f"v:app:{parts[2]}")]]),
            )
        elif cmd == "appwithdraw":
            await self.onboarding.review(actor, parts[2], int(parts[3]), "withdraw")
            await self.show_onboarding(actor, parts[2])
        elif cmd == "apsettings":
            row = await self.onboarding.get(actor, parts[2])
            policy = Policy.model_validate(row["initial_policy"])
            rows = [
                [
                    (
                        f"Проверять администраторов: {'нет' if policy.exempt_admins else 'да'}",
                        f"v:apvalue:{parts[2]}:{parts[3]}:admins",
                    )
                ],
                [("Назад", f"v:app:{parts[2]}")],
            ]
            await self.send(
                actor,
                "⚙️ Начальные настройки. Остальные параметры доступны после подключения в админской группе.",
                keyboard(rows),
            )
        elif cmd == "apvalue":
            row = await self.onboarding.get(actor, parts[2])
            policy = Policy.model_validate(row["initial_policy"])
            value = parts[4]
            if value != "admins":
                await self.show_onboarding(actor, parts[2])
                return True
            changes = {"exempt_admins": not policy.exempt_admins}
            await self.onboarding.configure(actor, parts[2], int(parts[3]), changes)
            await self.show_onboarding(actor, parts[2])
        return True

    async def policy_row(self, scope):
        async with self.engine.connect() as conn:
            await assert_scope(conn, scope, "read")
            return dict(
                (
                    await conn.execute(
                        select(t.group_settings, t.chats.c.title)
                        .join(t.chats, t.chats.c.id == t.group_settings.c.chat_id)
                        .where(t.group_settings.c.chat_id == scope.chat_id)
                    )
                )
                .mappings()
                .one()
            )

    async def execute_quick(self, scope, kind, payload, version=None):
        token = await self.operator.issue(scope, kind, payload, version)
        return await self.operator.confirm(scope.actor_id, scope.admin_chat_id, token)

    async def settings(self, scope, offset=0, section=None):
        from app.v2.ui import keyboard, SETTINGS, SETTING_SECTIONS, policy_value

        row = await self.policy_row(scope)
        await self.save_session(scope.actor_id, scope.admin_chat_id, scope.chat_id)
        policy = Policy.model_validate(row["policy"]).model_dump()
        gid, version = scope.chat_id, row["version"]
        editable = (
            scope.role in {Role.OWNER, Role.MANAGER} and scope.binding_version != 0
        )
        if section in {None, "trust"}:
            title = "⚙️ Настройки"
            hint = (
                "Автовайтлист действует только здесь. Уже выданное доверие сохраняется."
            )
            keys = (
                "auto_whitelist_enabled",
                "auto_whitelist_threshold",
                "exempt_admins",
            )
        else:
            title, hint, keys = SETTING_SECTIONS[section]
            keys = tuple(
                k
                for k in keys
                if not (
                    (k in {"mute_at", "mute_seconds"} and not policy["mute_enabled"])
                    or (k == "ban_at" and not policy["ban_enabled"])
                )
            )
        rows = []
        if section in {None, "trust"} and editable:
            rows.append([("Режим: " + label(row["mode"]), f"v:modes:{gid}")])
        if editable:
            for key in keys:
                if key == "daily_budget_usd":
                    continue
                caption = SETTINGS[key] + ": " + policy_value(key, policy)
                if key == "exempt_admins":
                    caption = "Проверять администраторов: " + (
                        "нет" if policy[key] else "да"
                    )
                rows.append([(caption, f"v:sp:{gid}:{key}:{version}")])
        else:
            hint += "\n\n" + "\n".join(
                SETTINGS[k] + ": " + policy_value(k, policy) for k in keys
            )
        if section in {None, "trust"}:
            rows += [
                [(caption, f"v:settingsgroup:{gid}:{key}")]
                for key, caption in (
                    ("sanctions", "Автоматические наказания"),
                    ("notifications", "Уведомления"),
                    ("advanced", "Дополнительно"),
                )
            ]
        else:
            rows.append([("Назад к настройкам", f"v:settings:{gid}:0")])
        rows.append([("Меню", f"v:menu:{gid}")])
        if row["mode"] != "auto" and section == "sanctions":
            hint += "\n👁 Автоматические наказания сейчас выключены."
        await self.panel(
            scope.admin_chat_id, f"{title} · {row['title']}\n\n{hint}", keyboard(rows)
        )

    async def apply_setting(self, scope, changes, expected_version):
        from app.v2.ui import SETTINGS, policy_value

        scope.require("settings")
        if "daily_budget_usd" in changes:
            raise Denied("💳 Бюджеты доступны только владельцу в личной панели.")
        row = await self.policy_row(scope)
        if row["version"] != expected_version:
            raise Conflict("⚠️ Настройки изменились. Откройте актуальную панель.")
        values = Policy.model_validate({**row["policy"], **changes}).model_dump()
        payload = {
            "changes": {k: values[k] for k in changes},
            "expected_policy_version": expected_version,
        }
        description = "\n".join(
            SETTINGS[k] + ": " + policy_value(k, values) for k in changes
        )
        if any(
            changes.get(k) is True and not row["policy"].get(k)
            for k in ("mute_enabled", "ban_enabled", "delete_when_admin_unavailable")
        ):
            return await self.confirmation(
                scope, "settings", payload, description=description
            )
        await self.execute_quick(scope, "settings", payload)
        await self.settings(scope)

    async def setting_picker(self, scope, key, version):
        from app.v2.ui import keyboard, setting_prompt

        scope.require("settings")
        if key == "daily_budget_usd":
            raise Denied("💳 Финансы — только в личной панели владельца.")
        row = await self.policy_row(scope)
        if row["version"] != version:
            return await self.settings(scope)
        policy = Policy.model_validate(row["policy"]).model_dump()
        if isinstance(policy[key], bool):
            return await self.apply_setting(scope, {key: not policy[key]}, version)
        values = PRESETS.get(key, [])
        if not values:
            return await self.setting_input(scope, key, version)
        rows = [
            [
                (
                    ("✓ " if policy[key] == value else "") + caption,
                    f"v:sv:{scope.chat_id}:{key}:{version}:{i}",
                )
                for i, (caption, value) in enumerate(values)
            ]
        ]
        rows += [
            [("Другое значение", f"v:si:{scope.chat_id}:{key}:{version}")],
            [("Назад", f"v:settings:{scope.chat_id}:0")],
        ]
        prompt = setting_prompt(key).split("\nИзменение применяется")[0]
        await self.panel(
            scope.admin_chat_id,
            f"⚙️ {row['title']}\n{prompt}\nВыбор сохраняется сразу.",
            keyboard(rows),
        )

    async def setting_input(self, scope, key, version):
        from app.v2.ui import keyboard, setting_prompt

        await self.save_session(
            scope.actor_id,
            scope.admin_chat_id,
            scope.chat_id,
            form="setting",
            field=key,
            expected_policy_version=version,
        )
        await self.send(
            scope.admin_chat_id,
            setting_prompt(key).replace(
                "Изменение применяется после подтверждения.",
                "Значение сохраняется сразу после проверки.",
            ),
            keyboard([[("Отмена", f"v:settings:{scope.chat_id}:0")]]),
        )

    async def mode_panel(self, scope):
        from app.v2.ui import keyboard

        scope.require("settings")
        row = await self.policy_row(scope)
        gid, mode, version = scope.chat_id, row["mode"], row["version"]
        policy = Policy.model_validate(row["policy"])
        lines = [f"🛡 Режим · {row['title']}", "Сейчас: " + label(mode)]
        if mode == "auto":
            lines += [
                "Наблюдение продолжит проверки без новых автоматических наказаний. Уже отправленные действия могут завершиться; ограничения сохранятся."
            ]
            rows = [[("Перейти в наблюдение", f"v:mv:{gid}:observe:{version}")]]
        elif mode == "paused":
            lines += [
                "Новые проверки остановлены. Возобновление не наказывает старые сообщения."
            ]
            rows = [[("▶️ Возобновить наблюдение", f"v:mv:{gid}:observe:{version}")]]
        else:
            lines += [
                "Наблюдение: проверки и карточки без автоматических наказаний.",
                f"Боевой: удаление скама по правилам; предупреждение после {policy.warn_at} страйка.",
                f"Мут: после {policy.mute_at}, на {policy.mute_seconds // 60} мин."
                if policy.mute_enabled
                else "Мут выключен.",
                f"Бан: после {policy.ban_at} страйков."
                if policy.ban_enabled
                else "Бан выключен.",
                "Кнопка включения — итоговое подтверждение. Доверие и история сохраняются.",
            ]
            rows = [[("🛡 Включить боевой режим", f"v:mv:{gid}:auto:{version}")]]
        if mode != "paused":
            rows.append([("⏸ Поставить на паузу", f"v:mv:{gid}:paused:{version}")])
        rows.append([("Назад", f"v:settings:{gid}:0")])
        await self.panel(scope.admin_chat_id, "\n\n".join(lines), keyboard(rows))

    async def member_action(
        self,
        scope,
        uid,
        action,
        version=None,
        days=None,
        grant=None,
        source=None,
        seconds=86400,
    ):
        if action in {"trust7", "trust30"}:
            days, action = int(action[5:]), "trust_add"
        row = await self.operator.profile(scope, uid)
        if version is not None and row["version"] != version:
            raise Conflict("👤 Профиль изменился. Откройте его заново.")
        payload = {
            "user_id": uid,
            "reason": "Проверенный участник"
            if action == "trust_add"
            else "Решение модератора",
            "days": days,
        }
        if source:
            payload.update(
                source_incident=str(source["id"]), source_version=source["version"]
            )
        if action == "mute":
            payload["seconds"] = seconds
        if grant:
            payload["grant_id"] = str(grant)
        if action == "trust_remove" and not grant:
            if not row.get("trust_entry"):
                raise Conflict("🤍 Активного доверия нет.")
            payload["grant_id"] = str(row["trust_entry"]["id"])
        if action in {"trust_add", "trust_remove"}:
            await self.execute_quick(scope, action, payload, row["version"])
            return await self.profile(scope, uid)
        description = DESCRIPTIONS[action]
        if action == "mute":
            description = (
                f"🔇 Запретить писать на {seconds // 60} мин. только в этой группе."
            )
        if action in {"unban", "unmute"} and row["deny"]:
            description += "\n⛔ Участник остаётся в блэклисте. Уберите его отдельно, если основание снято."
            payload["acknowledged_deny_id"] = str(row["deny_entry"]["id"])
        return await self.confirmation(
            scope, action, payload, row["version"], description
        )

    async def quick_card(self, scope, parts, message):
        from app.v2.ui import keyboard

        iid, version, action = parts[3], int(parts[4], 16), parts[5]
        row = await self.operator.detail(scope, iid)
        async with self.engine.connect() as conn:
            card = (
                await conn.execute(
                    select(t.cards.c.id).where(
                        t.cards.c.chat_id == scope.chat_id,
                        t.cards.c.incident_id == row["id"],
                        t.cards.c.admin_chat_id == scope.admin_chat_id,
                        t.cards.c.message_id == message.message_id,
                        t.cards.c.binding_version == scope.binding_version,
                    )
                )
            ).first()
        if not card:
            raise Conflict("🔄 Эта карточка устарела. Откройте её из истории.")
        if row["version"] != version:
            return await self.send(
                scope.admin_chat_id,
                "✏️ Сообщение или решение изменилось.",
                keyboard(
                    [[("Открыть актуальную карточку", f"v:card:{scope.chat_id}:{iid}")]]
                ),
            )
        if action in {"b", "w", "m", "x", "u"}:
            if not row["user_id"]:
                raise Conflict("👤 У этой публикации нет доступного профиля автора.")
            return await self.member_action(
                scope,
                row["user_id"],
                {
                    "b": "ban",
                    "w": "trust_add",
                    "m": "mute",
                    "x": "unban",
                    "u": "unmute",
                }[action],
                source=row,
            )
        if action == "n":
            await self.execute_quick(
                scope,
                "label",
                {"id": str(row["id"]), "label": "NOT_SCAM", "enforce": False},
                version,
            )
            return
        if action == "d":
            await self.execute_quick(scope, "delete", {"id": str(row["id"])}, version)
            return
        if action == "s":
            enforce = not row["legacy"] and not row["active_strike"]
            if not enforce:
                await self.execute_quick(
                    scope,
                    "label",
                    {"id": str(row["id"]), "label": "SCAM", "enforce": False},
                    version,
                )
                return
            policy = Policy.model_validate((await self.policy_row(scope))["policy"])
            await self.confirmation(
                scope,
                "label",
                {"id": str(row["id"]), "label": "SCAM", "enforce": True},
                version,
                "🚫 Подтвердить скам: удалить публикацию и учесть один страйк.\n"
                + f"Пороги: предупреждение {policy.warn_at}; "
                + (
                    f"мут {policy.mute_at} на {policy.mute_seconds // 60} мин.; "
                    if policy.mute_enabled
                    else "мут выключен; "
                )
                + (f"бан {policy.ban_at}." if policy.ban_enabled else "бан выключен."),
            )

    async def card_details(self, scope, iid):
        from app.v2.ui import keyboard

        row = await self.operator.detail(scope, iid)
        gid = scope.chat_id
        rows = [
            [("Полный текст и OCR", f"v:content:{gid}:{iid}")],
            [
                ("Изображения", f"v:images:{gid}:{iid}"),
                ("История решения", f"v:decisions:{gid}:{iid}"),
            ],
        ]
        if row["user_id"]:
            rows.append([("Участник", f"v:user:{gid}:{row['user_id']}")])
        if scope.role != Role.OBSERVER and scope.binding_version:
            rows.append([("Состояние действий Telegram", f"v:page:{gid}:actions")])
            if not row["legacy"]:
                rows.append([("Проверить снова", f"v:recheck:{gid}:{iid}")])
            if row["human_label"] == "NOT_SCAM":
                rows.append([("📄 Опубликовать копию", f"v:restore:{gid}:{iid}")])
        rows.append([("Назад к карточке", f"v:card:{gid}:{iid}")])
        await self.send(
            scope.admin_chat_id,
            "🔎 Подробности публикации · " + row["group_title"],
            keyboard(rows),
        )

    async def design_callback(self, scope, parts, query):
        from app.v2.ui import keyboard

        cmd = parts[1]
        if await self.team_callback(scope, parts):
            return True
        if await self.lifecycle_callback(scope, parts):
            return True
        if cmd == "q":
            await self.quick_card(scope, parts, query.message)
        elif cmd == "details":
            await self.card_details(scope, parts[3])
        elif cmd == "revise":
            row = await self.operator.detail(scope, parts[3])
            code = "n" if row["human_label"] == "SCAM" else "s"
            sent = await self.send(
                scope.admin_chat_id,
                "↩️ Изменить решение: " + row["group_title"],
                keyboard(
                    [
                        [
                            (
                                "Не скам" if code == "n" else "Скам",
                                f"v:q:{scope.chat_id}:{row['id'].hex}:{row['version']:x}:{code}",
                            )
                        ],
                        [("Назад", f"v:card:{scope.chat_id}:{row['id'].hex}")],
                    ]
                ),
            )
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
        elif cmd == "mp":
            await self.member_action(scope, int(parts[3]), parts[5], int(parts[4]))
        elif cmd == "mt":
            row = await self.operator.profile(scope, int(parts[3]))
            if row["version"] != int(parts[4]):
                raise Conflict("Профиль изменился. Откройте его заново.")
            base = f"v:mt:{scope.chat_id}:{row['user_id']}:{row['version']}"
            if parts[5] == "pick":
                await self.send(
                    scope.admin_chat_id,
                    "🔇 На сколько запретить участнику писать?",
                    keyboard(
                        [
                            [
                                (caption, base + f":{seconds}")
                                for caption, seconds in PRESETS["mute_seconds"]
                            ],
                            [("Другой срок", base + ":custom")],
                            [("Назад", f"v:user:{scope.chat_id}:{row['user_id']}")],
                        ]
                    ),
                )
            elif parts[5] == "custom":
                await self.save_session(
                    scope.actor_id,
                    scope.admin_chat_id,
                    scope.chat_id,
                    form="mute_custom",
                    user_id=row["user_id"],
                    object_version=row["version"],
                )
                await self.send(
                    scope.admin_chat_id,
                    "🔇 Пришлите срок в минутах: например 90. Затем будет короткое подтверждение.",
                    keyboard(
                        [[("Отмена", f"v:user:{scope.chat_id}:{row['user_id']}")]]
                    ),
                )
            else:
                await self.member_action(
                    scope, row["user_id"], "mute", row["version"], seconds=int(parts[5])
                )
        elif cmd == "pmore":
            row = await self.operator.profile(scope, int(parts[3]))
            base = f"v:mp:{scope.chat_id}:{row['user_id']}:{row['version']}"
            rows = [
                [
                    ("Вайтлист на 7 дней", base + ":trust7"),
                    ("Вайтлист на 30 дней", base + ":trust30"),
                ],
                [
                    (
                        "Убрать блэклист" if row["deny"] else "В блэклист",
                        base + (":deny_remove" if row["deny"] else ":deny_add"),
                    )
                ],
                [("Сбросить страйки", base + ":reset_strikes")],
                [
                    (
                        "Другой срок мута",
                        f"v:mt:{scope.chat_id}:{row['user_id']}:{row['version']}:pick",
                    )
                ],
                [("Назад", f"v:user:{scope.chat_id}:{row['user_id']}")],
            ]
            await self.send(
                scope.admin_chat_id, "👤 Дополнительные действия", keyboard(rows)
            )
        elif cmd == "tgremove":
            async with self.engine.connect() as conn:
                grant = (
                    (
                        await conn.execute(
                            select(t.trust).where(
                                t.trust.c.id == uuid.UUID(parts[3]),
                                t.trust.c.chat_id == scope.chat_id,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
            if not grant or grant["revoked_at"]:
                raise Conflict("🤍 Эта выдача доверия уже не действует.")
            await self.member_action(
                scope, grant["user_id"], "trust_remove", grant=grant["id"]
            )
        elif cmd == "sp":
            await self.setting_picker(scope, parts[3], int(parts[4]))
        elif cmd == "si":
            scope.require("settings")
            await self.setting_input(scope, parts[3], int(parts[4]))
        elif cmd == "sv":
            value = PRESETS[parts[3]][int(parts[5])][1]
            await self.apply_setting(scope, {parts[3]: value}, int(parts[4]))
        elif cmd == "mv":
            scope.require("settings")
            payload = {"mode": parts[3], "expected_policy_version": int(parts[4])}
            if parts[3] == "paused":
                await self.confirmation(
                    scope,
                    "mode",
                    payload,
                    description="⏸ Остановить новые проверки и автоматические действия. История, доверие и действующие ограничения сохраняются.",
                )
            else:
                await self.execute_quick(scope, "mode", payload)
                await self.mode_panel(scope)
        elif cmd == "more":
            gid = scope.chat_id
            rows = [
                [("Блэклист", f"v:page:{gid}:deny"), ("Статистика", f"v:stats:{gid}")],
                [("Состояние", f"v:status:{gid}"), ("Журнал", f"v:page:{gid}:audit")],
                [("Действия Telegram", f"v:page:{gid}:actions")],
            ]
            if scope.role in {Role.MANAGER, Role.OWNER}:
                rows += [
                    [("Команда", f"v:page:{gid}:roles"), ("Режим", f"v:modes:{gid}")]
                ]
                rows.append([("Подключение и управление", f"v:life:{gid}")])
            rows += [[("Выбрать группу", "v:choose"), ("Меню", f"v:menu:{gid}")]]
            await self.send(scope.admin_chat_id, "⋯ Дополнительно", keyboard(rows))
        else:
            return False
        return True
