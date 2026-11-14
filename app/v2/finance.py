"""Financial controls belong to the system owner in a private chat only."""

import secrets
import math
from datetime import timedelta, datetime

from sqlalchemy import select, func, text
from sqlalchemy.dialects.postgresql import insert

from app.v2 import tables as t
from app.v2.auth import owner
from app.v2.domain import Policy, Denied, Conflict, now
from app.v2.state import audit


async def global_state(conn, fallback=None):
    value = (
        await conn.execute(
            select(t.system.c.value).where(t.system.c.key == "global_budget")
        )
    ).scalar_one_or_none()
    return value if value is not None else {"amount": fallback, "version": 0}


async def global_overview(engine, actor, destination, fallback=None):
    async with engine.connect() as conn:
        await authorize(conn, actor, destination)
        state = await global_state(conn, fallback)
        used = (
            await conn.execute(
                select(
                    func.coalesce(
                        func.sum(func.coalesce(t.budget.c.spent, t.budget.c.reserved)),
                        0,
                    )
                ).where(t.budget.c.day == now().date().isoformat())
            )
        ).scalar_one()
        return {**state, "used": used}


async def prepare_global(engine, actor, destination, value, fallback=None):
    amount = Policy(daily_budget_usd=value).daily_budget_usd
    if amount is not None and not math.isfinite(amount):
        raise Conflict("Лимит должен быть конечным положительным числом.")
    async with engine.begin() as conn:
        await authorize(conn, actor, destination)
        state = await global_state(conn, fallback)
        token = secrets.token_urlsafe(18)
        await conn.execute(
            t.system.insert().values(
                key="budget-confirm:" + token,
                value={
                    "actor": actor,
                    "amount": amount,
                    "version": state["version"],
                    "previous": state["amount"],
                    "expires": (now() + timedelta(minutes=5)).isoformat(),
                },
            )
        )
        return token


async def confirm_global(engine, actor, destination, token, fallback=None):
    async with engine.begin() as conn:
        await authorize(conn, actor, destination)
        # Serialize changes with reservations across every worker and group.
        await conn.execute(text("SELECT pg_advisory_xact_lock(71392144)"))
        op = (
            await conn.execute(
                select(t.system.c.value)
                .where(t.system.c.key == "budget-confirm:" + token)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not op or op["actor"] != actor:
            raise Denied("Подтверждение недоступно.")
        if op.get("used"):
            return op["result"]
        if datetime.fromisoformat(op["expires"]) <= now():
            raise Conflict("Подтверждение истекло. Задайте бюджет заново.")
        state = await global_state(conn, fallback)
        if state["version"] != op["version"] or state["amount"] != op["previous"]:
            raise Conflict("Общий лимит изменился. Проверьте его заново.")
        stmt = insert(t.system).values(
            key="global_budget",
            value={"amount": op["amount"], "version": state["version"] + 1},
            updated_at=now(),
        )
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={"value": stmt.excluded.value, "updated_at": now()},
            )
        )
        # Wake budget-deferred checks only; never retry uncertain provider requests.
        await conn.execute(
            t.jobs.update()
            .where(
                t.jobs.c.kind == "check",
                t.jobs.c.state == "pending",
                t.jobs.c.last_error == "daily_budget",
            )
            .values(due_at=now())
        )
        result = "Общий лимит сохранён. Расход и резервы сохранены; лимиты групп продолжают действовать."
        await conn.execute(
            t.system.update()
            .where(t.system.c.key == "budget-confirm:" + token)
            .values(value={**op, "used": True, "result": result}, updated_at=now())
        )
        await audit(conn, None, actor, "owner.global_budget", amount=op["amount"])
        return result


async def cancel_global(engine, actor, destination, token):
    async with engine.begin() as conn:
        await authorize(conn, actor, destination)
        key = "budget-confirm:" + token
        op = (
            await conn.execute(
                select(t.system.c.value).where(t.system.c.key == key).with_for_update()
            )
        ).scalar_one_or_none()
        if not op or op["actor"] != actor:
            raise Denied("Подтверждение недоступно.")
        if not op.get("used"):
            await conn.execute(
                t.system.update()
                .where(t.system.c.key == key)
                .values(
                    value={**op, "used": True, "result": "Изменение отменено."},
                    updated_at=now(),
                )
            )


async def authorize(conn, actor, destination):
    if destination != actor or not await owner(conn, actor):
        raise Denied("Бюджеты доступны только владельцу бота в личном чате.")


async def overview(engine, actor, destination, offset=0):
    if not 0 <= offset <= 100000:
        raise Conflict("Откройте список бюджетов заново.")
    async with engine.connect() as conn:
        await authorize(conn, actor, destination)
        usage = (
            select(
                t.budget.c.chat_id,
                func.sum(func.coalesce(t.budget.c.spent, t.budget.c.reserved)).label(
                    "used"
                ),
            )
            .where(t.budget.c.day == now().date().isoformat())
            .group_by(t.budget.c.chat_id)
            .subquery()
        )
        rows = (
            (
                await conn.execute(
                    select(
                        t.chats.c.id,
                        t.chats.c.title,
                        t.group_settings.c.policy,
                        func.coalesce(usage.c.used, 0).label("used"),
                    )
                    .join(t.group_settings, t.group_settings.c.chat_id == t.chats.c.id)
                    .outerjoin(usage, usage.c.chat_id == t.chats.c.id)
                    .where(t.group_settings.c.lifecycle == "connected")
                    .order_by(t.chats.c.id)
                    .offset(offset)
                    .limit(21)
                )
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows[:20]], len(rows) > 20


async def prepare(engine, actor, destination, gid, value):
    # Policy validates finite positive amounts. None removes only the group limit.
    amount = Policy(daily_budget_usd=value).daily_budget_usd
    if amount is not None and not math.isfinite(amount):
        raise Conflict("Лимит должен быть конечным положительным числом.")
    async with engine.begin() as conn:
        await authorize(conn, actor, destination)
        settings = (
            (
                await conn.execute(
                    select(t.group_settings).where(t.group_settings.c.chat_id == gid)
                )
            )
            .mappings()
            .one()
        )
        if settings["lifecycle"] != "connected":
            raise Conflict("Группа не подключена.")
        token = secrets.token_urlsafe(18)
        await conn.execute(
            t.operations.insert().values(
                token=token,
                actor_id=actor,
                admin_chat_id=actor,
                chat_id=gid,
                binding_version=0,
                kind="owner_budget",
                payload={"amount": amount, "policy_version": settings["version"]},
                expires_at=now() + timedelta(minutes=5),
            )
        )
        return token


async def confirm(engine, actor, destination, token):
    async with engine.begin() as conn:
        await authorize(conn, actor, destination)
        op = (
            (
                await conn.execute(
                    select(t.operations)
                    .where(
                        t.operations.c.token == token,
                        t.operations.c.actor_id == actor,
                        t.operations.c.admin_chat_id == actor,
                        t.operations.c.kind == "owner_budget",
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if not op:
            raise Denied("Подтверждение недоступно.")
        if op["used_at"]:
            return op["result"]["message"]
        if op["expires_at"] <= now():
            raise Conflict("Подтверждение истекло. Задайте бюджет заново.")
        group = (
            (
                await conn.execute(
                    select(t.group_settings)
                    .where(t.group_settings.c.chat_id == op["chat_id"])
                    .with_for_update()
                )
            )
            .mappings()
            .one()
        )
        if (
            group["version"] != op["payload"]["policy_version"]
            or group["lifecycle"] != "connected"
        ):
            raise Conflict(
                "Настройки или подключение группы изменились. Проверьте бюджет заново."
            )
        policy = Policy.model_validate(
            {**group["policy"], "daily_budget_usd": op["payload"]["amount"]}
        )
        await conn.execute(
            t.group_settings.update()
            .where(t.group_settings.c.chat_id == op["chat_id"])
            .values(
                policy=policy.model_dump(),
                version=group["version"] + 1,
                updated_at=now(),
            )
        )
        result = {
            "message": "Лимит группы сохранён. Общий лимит бота продолжает действовать."
        }
        await conn.execute(
            t.operations.update()
            .where(t.operations.c.token == token)
            .values(used_at=now(), result=result)
        )
        await audit(
            conn, op["chat_id"], actor, "owner.budget", amount=policy.daily_budget_usd
        )
        return result["message"]
