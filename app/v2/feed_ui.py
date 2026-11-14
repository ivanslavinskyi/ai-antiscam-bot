"""Shared-admin history with a personal filter and explicitly scoped SQL."""

from datetime import timedelta

from sqlalchemy import select, or_, exists

from app.v2 import tables as t
from app.v2.auth import assert_scope
from app.v2.domain import Denied, GroupScope, Role, now
from app.v2.presentation import person, label


class FeedControls:
    async def feed_rows(self, actor, admin, filters=None, offset=0):
        filters = filters or {}
        groups = await self.auth.groups(actor, admin)
        if not groups:
            raise Denied("Нет доступных рабочих групп в этой админке.")
        # The actor must still be in the admin chat, then validate each active binding in SQL.
        await self.auth.scope(actor, admin, groups[0]["id"])
        ids = [g["id"] for g in groups]
        if filters.get("group"):
            ids = [gid for gid in ids if gid == filters["group"]]
        if not ids:
            return [], False
        async with self.engine.connect() as conn:
            bindings = (
                (
                    await conn.execute(
                        select(t.bindings).where(
                            t.bindings.c.chat_id.in_(ids),
                            t.bindings.c.admin_chat_id == admin,
                            t.bindings.c.closed_at.is_(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
            for binding in bindings:
                await assert_scope(
                    conn,
                    GroupScope(
                        binding["chat_id"],
                        actor,
                        admin,
                        binding["version"],
                        Role.OBSERVER,
                    ),
                    "read",
                )
            ids = [b["chat_id"] for b in bindings]
            query = (
                select(
                    t.incidents,
                    t.chats.c.title.label("group_title"),
                    t.users.c.first_name,
                    t.users.c.last_name,
                    t.users.c.username,
                    t.users.c.telegram_user_id,
                )
                .join(t.chats, t.chats.c.id == t.incidents.c.chat_id)
                .outerjoin(t.users, t.users.c.id == t.incidents.c.user_id)
                .where(t.incidents.c.chat_id.in_(ids))
            )
            if filters.get("review"):
                query = query.where(
                    t.incidents.c.human_label.is_(None),
                    t.incidents.c.status.in_(["review", "error", "checked"]),
                    or_(
                        t.incidents.c.model_label == "SCAM",
                        t.incidents.c.status.in_(["review", "error"]),
                    ),
                )
            if filters.get("days"):
                query = query.where(
                    t.incidents.c.created_at
                    >= now() - timedelta(days=min(365, int(filters["days"])))
                )
            if filters.get("label") == "SCAM":
                query = query.where(
                    or_(
                        t.incidents.c.human_label == "SCAM",
                        (
                            t.incidents.c.human_label.is_(None)
                            & (t.incidents.c.model_label == "SCAM")
                        ),
                    )
                )
            elif filters.get("label") == "NOT_SCAM":
                query = query.where(
                    or_(
                        t.incidents.c.human_label == "NOT_SCAM",
                        (
                            t.incidents.c.human_label.is_(None)
                            & (t.incidents.c.model_label == "OK")
                        ),
                    )
                )
            if filters.get("error"):
                query = query.where(t.incidents.c.status == "error")
            text = filters.get("query", "")[:100]
            if text:
                text_match = exists(
                    select(t.items.c.id)
                    .join(
                        t.revisions,
                        (t.revisions.c.item_id == t.items.c.id)
                        & (t.revisions.c.revision == t.items.c.revision),
                    )
                    .where(
                        t.items.c.chat_id == t.incidents.c.chat_id,
                        t.items.c.incident_key == t.incidents.c.incident_key,
                        or_(
                            t.revisions.c.text.icontains(text, autoescape=True),
                            t.revisions.c.ocr_text.icontains(text, autoescape=True),
                        ),
                    )
                )
                query = query.where(
                    or_(
                        t.users.c.first_name.icontains(text, autoescape=True),
                        t.users.c.last_name.icontains(text, autoescape=True),
                        t.users.c.username.icontains(text.lstrip("@"), autoescape=True),
                        text_match,
                    )
                )
            rows = (
                (
                    await conn.execute(
                        query.order_by(
                            t.incidents.c.created_at.desc(), t.incidents.c.id.desc()
                        )
                        .offset(max(0, min(offset, 100000)))
                        .limit(11)
                    )
                )
                .mappings()
                .all()
            )
            return [dict(r) for r in rows[:10]], len(rows) > 10

    async def admin_home(self, actor, admin):
        from app.v2.ui import keyboard

        groups = await self.auth.groups(actor, admin)
        if not groups:
            raise Denied("Нет доступных рабочих групп.")
        await self.auth.scope(actor, admin, groups[0]["id"])
        await self.save_session(actor, admin)
        rows = [
            [("Неразобранные", "v:feed:review:0"), ("История", "v:feed:history:0")],
            [("Участники", "v:target:members"), ("Вайтлист", "v:target:trust")],
            [("Группы и настройки", "v:target:settings")],
            [("Ещё", "v:target:more")],
        ]
        await self.send(
            admin,
            f"🛡 Управление модерацией\n👥 Доступных рабочих групп: {len(groups)}\nКарточки приходят в общую ленту. Каждое действие относится к группе карточки.",
            keyboard(rows),
        )

    async def feed(self, actor, admin, section="history", offset=0):
        from app.v2.ui import keyboard

        state = await self.session(actor, admin)
        filters = state["state"].get("feed_filters", {}) if state else {}
        rows, more = await self.feed_rows(
            actor, admin, {**filters, "review": section == "review"}, offset
        )
        buttons = []
        for row in rows:
            icon = (
                "✅"
                if row["human_label"] == "NOT_SCAM"
                or not row["human_label"]
                and row["model_label"] == "OK"
                else "🚫"
                if row["human_label"] == "SCAM"
                else "⚠️"
            )
            buttons.append(
                [
                    (
                        f"{icon} {row['group_title'][:25]} · {person(row)[:30]} · {row['created_at'].strftime('%d.%m %H:%M')}",
                        f"v:card:{row['chat_id']}:{row['id'].hex}",
                    )
                ]
            )
        if offset or more:
            buttons.append(
                ([("Ранее", f"v:feed:{section}:{offset + 10}")] if more else [])
                + (
                    [("Позже", f"v:feed:{section}:{max(0, offset - 10)}")]
                    if offset
                    else []
                )
            )
        buttons += [
            [("Поиск", "v:feedsearch"), ("🎛 Фильтры", "v:feedfilters")],
            [("Меню", "v:home")],
        ]
        captions = {
            "query": "Поиск",
            "days": "За дней",
            "label": "Результат",
            "error": "Только ошибки",
            "group": "Группа",
        }
        titles = {g["id"]: g["title"] for g in await self.auth.groups(actor, admin)}
        hint = "\n" + (
            " · ".join(
                f"{captions[key]}: {titles.get(value, value) if key == 'group' else 'да' if key == 'error' else label(value)}"
                for key, value in filters.items()
                if key in captions
            )
            or "Все доступные группы · весь период"
        )
        await self.send(
            admin,
            ("📜 История" if section == "history" else "⚠️ Неразобранные")
            + hint
            + (
                "\nНичего не найдено. Измените поиск или сбросьте фильтры."
                if not rows
                else ""
            ),
            keyboard(buttons),
        )

    async def feed_callback(self, actor, admin, parts):
        from app.v2.ui import keyboard

        cmd = parts[1]
        if cmd not in {
            "home",
            "feed",
            "target",
            "feedsearch",
            "feedfilters",
            "ff",
            "fg",
        }:
            return False
        if actor == admin:
            raise Denied("Общая лента доступна в связанной админской группе.")
        groups = await self.auth.groups(actor, admin)
        if not groups:
            raise Denied("Нет доступных групп.")
        await self.auth.scope(actor, admin, groups[0]["id"])
        if cmd == "home":
            await self.admin_home(actor, admin)
        elif cmd == "feed":
            await self.feed(actor, admin, parts[2], int(parts[3]))
        elif cmd == "target":
            if len(parts) == 4 or len(groups) == 1:
                gid = int(parts[3]) if len(parts) == 4 else groups[0]["id"]
                scope = await self.auth.scope(actor, admin, gid)
                if parts[2] == "settings":
                    await self.settings(scope)
                elif parts[2] == "members":
                    await self.participants(scope)
                elif parts[2] == "trust":
                    await self.page(scope, "trust")
                elif parts[2] == "more":
                    await self.design_callback(scope, ["v", "more", str(gid)], None)
            else:
                await self.send(
                    admin,
                    "👥 Выберите рабочую группу",
                    keyboard(
                        [
                            [(g["title"], f"v:target:{parts[2]}:{g['id']}")]
                            for g in groups
                        ]
                        + [[("Меню", "v:home")]]
                    ),
                )
        elif cmd == "feedsearch":
            state = await self.session(actor, admin)
            filters = state["state"].get("feed_filters", {}) if state else {}
            await self.save_session(
                actor, admin, form="feedsearch", feed_filters=filters
            )
            await self.send(
                admin,
                "🔍 Напишите имя, username или фрагмент сообщения.",
                keyboard([[("Отмена", "v:home")]]),
            )
        elif cmd == "feedfilters":
            rows = [
                [
                    ("Сегодня", "v:ff:day"),
                    ("7 дней", "v:ff:week"),
                    ("30 дней", "v:ff:month"),
                ],
                [
                    ("Скам", "v:ff:scam"),
                    ("Не скам", "v:ff:safe"),
                    ("Ошибки", "v:ff:errors"),
                ],
            ]
            rows += [[(g["title"], f"v:fg:{g['id']}")] for g in groups]
            rows += [
                [
                    ("🧹 Сбросить", "v:ff:clear"),
                    ("Показать результаты", "v:feed:history:0"),
                ]
            ]
            await self.send(admin, "🎛 Фильтры истории", keyboard(rows))
        else:
            state = await self.session(actor, admin)
            filters = state["state"].get("feed_filters", {}) if state else {}
            if cmd == "fg":
                if int(parts[2]) not in {g["id"] for g in groups}:
                    raise Denied("Группа недоступна.")
                filters["group"] = int(parts[2])
            elif parts[2] == "clear":
                filters = {}
            elif parts[2] in {"scam", "safe", "errors"}:
                filters.pop("label", None)
                filters.pop("error", None)
                filters.update(
                    {"error": True}
                    if parts[2] == "errors"
                    else {"label": "SCAM" if parts[2] == "scam" else "NOT_SCAM"}
                )
            else:
                filters["days"] = {"day": 1, "week": 7, "month": 30}[parts[2]]
            await self.save_session(actor, admin, feed_filters=filters)
            await self.feed(actor, admin)
        return True
