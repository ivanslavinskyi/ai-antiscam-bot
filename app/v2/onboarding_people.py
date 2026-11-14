"""Owner-only people selection: Telegram picker and explicit numeric IDs."""

import secrets

from aiogram.types import (
    KeyboardButton,
    KeyboardButtonRequestUsers,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy import select

from app.v2 import tables as t
from app.v2.auth import owner
from app.v2.domain import Conflict, Denied, now

PEOPLE_ROLES = {
    "manager": "Управляющий",
    "work": "Подтверждает рабочую группу",
    "admin": "Подтверждает админскую группу",
}


def valid_person(value):
    if isinstance(value, bool):
        raise Conflict("Нужен положительный Telegram ID человека.")
    try:
        result = int(str(value).strip())
    except ValueError as exc:
        raise Conflict(
            "Пришлите числовой Telegram ID или выберите человека кнопкой."
        ) from exc
    if not 0 < result < 2**52:
        raise Conflict("Нужен положительный Telegram ID человека.")
    return result


class PeopleControls:
    async def people_state(self, conn, actor, nonce=None, lock=False):
        if not await owner(conn, actor):
            raise Denied("Выбор других подтверждающих доступен владельцу бота.")
        stmt = select(t.sessions).where(
            t.sessions.c.actor_id == actor,
            t.sessions.c.admin_chat_id == actor,
            t.sessions.c.expires_at > now(),
        )
        row = (
            (await conn.execute(stmt.with_for_update() if lock else stmt))
            .mappings()
            .first()
        )
        if (
            not row
            or row["state"].get("form") != "newpeople"
            or not row["state"].get("people")
            or row["state"].get("created_request")
            or (nonce and row["state"].get("draft") != nonce)
        ):
            raise Conflict("Выбор людей устарел. Начните подключение заново.")
        return dict(row["state"])

    async def people_start(self, actor):
        if not await self.auth.is_owner(actor):
            raise Denied("Нужна роль владельца.")
        await self.save_session(
            actor,
            actor,
            form="newpeople",
            draft=secrets.token_hex(6),
            people={side: {"id": actor, "name": "Вы"} for side in PEOPLE_ROLES},
        )
        await self.people_home(actor)

    async def people_home(self, actor):
        from app.v2.ui import keyboard

        async with self.engine.connect() as conn:
            state = await self.people_state(conn, actor)
        lines = ["👥 Кто подключает группы", ""]
        buttons = []
        for side, caption in PEOPLE_ROLES.items():
            person = state["people"][side]
            lines.append(f"{caption}: {person['name']} ({person['id']})")
            buttons.append([(caption, f"v:peoplepick:{state['draft']}:{side}")])
        lines += [
            "",
            "По умолчанию везде вы. Нажмите нужную роль, чтобы выбрать другого человека или вставить его ID.",
            "Быстрый ввод: три ID через пробел — управляющий, рабочая, админская.",
            "Выбор человека ещё не выдаёт доступ. Права проверяются при подключении групп.",
        ]
        buttons += [
            [("Продолжить с этими людьми", f"v:peopledone:{state['draft']}")],
            [("Отмена", "v:peoplecancel")],
        ]
        await self.send(actor, "\n".join(lines), keyboard(buttons))

    async def people_callback(self, actor, admin, parts):
        cmd = parts[1]
        if cmd not in {"peoplepick", "peopledone", "peoplecancel"}:
            return False
        if actor != admin or not await self.auth.is_owner(actor):
            raise Denied("Выбор людей доступен владельцу в личке бота.")
        if cmd == "peoplecancel":
            await self.save_session(actor, actor)
            await self.send(actor, "Выбор отменён.", ReplyKeyboardRemove())
            await self.system(actor)
            return True
        async with self.engine.begin() as conn:
            state = await self.people_state(conn, actor, parts[2], lock=True)
            if cmd == "peoplepick":
                side = parts[3]
                if side not in PEOPLE_ROLES:
                    raise Conflict("Неизвестная роль.")
                request_id = secrets.randbelow(2**31 - 1)
                state.update(side=side, request_id=request_id)
                await conn.execute(
                    t.sessions.update()
                    .where(
                        t.sessions.c.actor_id == actor,
                        t.sessions.c.admin_chat_id == actor,
                    )
                    .values(state=state)
                )
        if cmd == "peopledone":
            ids = [state["people"][side]["id"] for side in PEOPLE_ROLES]
            rid = await self.onboarding.create(actor, *ids, people_draft=state["draft"])
            await self.send(
                actor, "Люди выбраны. Перейдём к группам.", ReplyKeyboardRemove()
            )
            await self.show_onboarding(actor, rid)
            return True
        await self.send(
            actor,
            f"👤 {PEOPLE_ROLES[side]}\nВыберите человека кнопкой Telegram или пришлите его числовой ID. Можно выбрать себя кнопкой «Я».",
            ReplyKeyboardMarkup(
                keyboard=[
                    [
                        KeyboardButton(
                            text="Выбрать человека",
                            request_users=KeyboardButtonRequestUsers(
                                request_id=request_id,
                                user_is_bot=False,
                                max_quantity=1,
                                request_name=True,
                                request_username=True,
                            ),
                        )
                    ],
                    [KeyboardButton(text="Я"), KeyboardButton(text="Назад к людям")],
                ],
                resize_keyboard=True,
            ),
        )
        return True

    async def people_message(self, message):
        actor = message.from_user.id
        if message.chat.id != actor:
            return False
        session = await self.session(actor, actor)
        if not session or session["state"].get("form") != "newpeople":
            if message.users_shared:
                raise Conflict("Выбор устарел. Откройте подключение заново.")
            return False
        text = (message.text or "").strip()
        if text.startswith("/"):
            return False
        async with self.engine.begin() as conn:
            state = await self.people_state(conn, actor, lock=True)
            if text == "Назад к людям":
                state.pop("request_id", None)
                state.pop("side", None)
            else:
                side = state.get("side")
                if message.users_shared:
                    shared = message.users_shared
                    if (
                        not side
                        or shared.request_id != state.get("request_id")
                        or len(shared.users) != 1
                    ):
                        raise Conflict("Этот выбор устарел. Выберите человека заново.")
                    user = shared.users[0]
                    uid = valid_person(user.user_id)
                    name = " ".join(filter(None, [user.first_name, user.last_name]))
                    name = " ".join(name.split())[:80] or user.username or "Участник"
                    selected = {side: {"id": uid, "name": name}}
                elif side and text == "Я":
                    selected = {side: {"id": actor, "name": "Вы"}}
                else:
                    values = text.split()
                    roles = (
                        list(PEOPLE_ROLES)
                        if len(values) == 3
                        else [side]
                        if side and len(values) == 1
                        else []
                    )
                    if not roles:
                        raise Conflict(
                            "Выберите роль кнопкой или пришлите три ID через пробел."
                        )
                    selected = {}
                    for role, value in zip(roles, values):
                        uid = valid_person(value)
                        name = (
                            await conn.execute(
                                select(t.admin_contacts.c.name)
                                .where(t.admin_contacts.c.actor_id == uid)
                                .order_by(t.admin_contacts.c.updated_at.desc())
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                        selected[role] = {
                            "id": uid,
                            "name": "Вы"
                            if uid == actor
                            else " ".join((name or "Участник").split())[:80],
                        }
                if any(person["id"] == self.bot.id for person in selected.values()):
                    raise Conflict("Выберите человека, а не самого бота.")
                state["people"] = {**state["people"], **selected}
                state.pop("request_id", None)
                state.pop("side", None)
            await conn.execute(
                t.sessions.update()
                .where(
                    t.sessions.c.actor_id == actor, t.sessions.c.admin_chat_id == actor
                )
                .values(state=state)
            )
        await self.send(actor, "Выбор сохранён.", ReplyKeyboardRemove())
        await self.people_home(actor)
        return True
