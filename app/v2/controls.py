import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.v2 import tables as t
from app.v2.auth import owner
from app.v2.domain import Denied, Conflict, now
from app.v2.state import audit


async def flags(conn, lock=False):
    query = select(t.system.c.value).where(t.system.c.key == "controls")
    if lock:
        query = query.with_for_update()
    return (await conn.execute(query)).scalar_one_or_none() or {}


async def heartbeat(engine, component, **value):
    async with engine.begin() as conn:
        stmt = insert(t.system).values(
            key="heartbeat:" + component, value=value, updated_at=now()
        )
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["key"], set_={"value": value, "updated_at": now()}
            )
        )


async def prepare(engine, actor, paused):
    async with engine.begin() as conn:
        if not await owner(conn, actor):
            raise Denied("Нужна роль владельца.")
        token = secrets.token_urlsafe(18)
        await conn.execute(
            t.system.insert().values(
                key="confirm:" + token,
                value={
                    "actor": actor,
                    "paused": paused,
                    "expires": (now() + timedelta(minutes=5)).isoformat(),
                },
            )
        )
        return token


async def confirm(engine, actor, token):
    from datetime import datetime

    async with engine.begin() as conn:
        if not await owner(conn, actor):
            raise Denied("Нужна роль владельца.")
        row = (
            (
                await conn.execute(
                    select(t.system)
                    .where(t.system.c.key == "confirm:" + token)
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if (
            not row
            or row["value"]["actor"] != actor
            or datetime.fromisoformat(row["value"]["expires"]) <= now()
        ):
            raise Conflict("Системное подтверждение недействительно.")
        if row["value"].get("used"):
            return
        value = {**(await flags(conn)), "automatic_paused": row["value"]["paused"]}
        stmt = insert(t.system).values(key="controls", value=value, updated_at=now())
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["key"], set_={"value": value, "updated_at": now()}
            )
        )
        if value["automatic_paused"]:
            await conn.execute(
                t.actions.update()
                .where(t.actions.c.actor_id.is_(None), t.actions.c.state == "planned")
                .values(state="cancelled", error="system_paused")
            )
        await conn.execute(
            t.system.update()
            .where(t.system.c.key == row["key"])
            .values(value={**row["value"], "used": True})
        )
        await audit(
            conn,
            None,
            actor,
            "system.automatic_paused",
            paused=value["automatic_paused"],
        )
