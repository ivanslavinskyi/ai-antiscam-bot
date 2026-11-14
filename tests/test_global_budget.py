import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, func

from app.v2 import finance, tables as t
from app.v2.domain import Conflict, Denied, now
from app.v2.llm import Budget, RetryLater
from app.v2.config import Settings
from app.v2.maintenance import Maintenance
from app.v2.queue import enqueue
from test_people_selection import make_ui, click, private


async def set_limit(db, amount, fallback=0.25):
    token = await finance.prepare_global(db, 111, 111, amount, fallback)
    return await finance.confirm_global(db, 111, 111, token, fallback)


async def test_global_limit_live_change_preserves_usage_and_group_caps(db):
    await Budget(db, 0.25).reserve(1, "before", 0.2, None)
    await set_limit(db, 0.1)
    with pytest.raises(RetryLater):
        await Budget(db, 10).reserve(2, "blocked", 0.01, None)
    assert (await finance.global_overview(db, 111, 111))["used"] == 0.2
    await set_limit(db, None)
    with pytest.raises(RetryLater):
        await Budget(db, 0.01).reserve(1, "local-block", 0.01, 0.1)
    await Budget(db, 0.01).reserve(2, "allowed", 0.1, 0.2)
    async with db.connect() as conn:
        assert (
            await conn.execute(select(func.count()).select_from(t.budget))
        ).scalar_one() == 2
        assert set(
            (await conn.execute(select(t.group_settings.c.version))).scalars()
        ) == {1}


async def test_global_confirm_atomic_idempotent_and_stale(db):
    tokens = [await finance.prepare_global(db, 111, 111, a) for a in (0.5, 1)]
    results = await asyncio.gather(
        *[finance.confirm_global(db, 111, 111, token) for token in tokens],
        return_exceptions=True,
    )
    assert sum(isinstance(r, Conflict) for r in results) == 1
    index = next(i for i, r in enumerate(results) if isinstance(r, str))
    assert await finance.confirm_global(db, 111, 111, tokens[index]) == results[index]


async def test_global_cancel_expiry_invalid_values_and_owner_boundaries(db):
    for actor, dest in [(222, 222), (111, -2001)]:
        with pytest.raises(Denied):
            await finance.prepare_global(db, actor, dest, 1)
    for value in (-1, 0, float("inf"), float("nan")):
        with pytest.raises((ValueError, Conflict)):
            await finance.prepare_global(db, 111, 111, value)
    token = await finance.prepare_global(db, 111, 111, 1)
    await finance.cancel_global(db, 111, 111, token)
    assert "отменено" in await finance.confirm_global(db, 111, 111, token)
    token = await finance.prepare_global(db, 111, 111, 1)
    async with db.begin() as conn:
        key = "budget-confirm:" + token
        op = (
            await conn.execute(select(t.system.c.value).where(t.system.c.key == key))
        ).scalar_one()
        await conn.execute(
            t.system.update()
            .where(t.system.c.key == key)
            .values(value={**op, "expires": (now() - timedelta(seconds=1)).isoformat()})
        )
    with pytest.raises(Conflict):
        await finance.confirm_global(db, 111, 111, token)
    token = await finance.prepare_global(db, 111, 111, 1)
    async with db.begin() as conn:
        await conn.execute(
            t.roles.update().where(t.roles.c.actor_id == 111).values(revoked_at=now())
        )
    with pytest.raises(Denied):
        await finance.confirm_global(db, 111, 111, token)


async def test_owner_global_budget_manual_ui(db, bot, auth):
    ui = make_ui(db, bot, auth)
    ui.global_budget_default = 0.25
    await click(ui, "v:gbudget")
    await ui.message(private("0,75"))
    async with db.connect() as conn:
        key = (
            await conn.execute(
                select(t.system.c.key).where(t.system.c.key.like("budget-confirm:%"))
            )
        ).scalar_one()
    assert (await finance.global_overview(db, 111, 111, 0.25))["amount"] == 0.25
    await click(ui, "v:gbudgetconfirm:" + key.split(":", 1)[1])
    assert (await finance.global_overview(db, 111, 111, 0.25))["amount"] == 0.75
    assert all(c[1] == 111 for c in bot.calls if c[0] == "send_message")


async def test_global_budget_wakes_only_budget_waits_and_alert_uses_override(db, bot):
    future = now() + timedelta(hours=1)
    async with db.begin() as conn:
        for key, error in [
            ("budget-wait", "daily_budget"),
            ("uncertain", "provider_unknown"),
        ]:
            await enqueue(conn, "check", key, {}, due_at=future)
            await conn.execute(
                t.jobs.update().where(t.jobs.c.key == key).values(last_error=error)
            )
    await Budget(db, 1).reserve(1, "used", 0.09, None)
    await set_limit(db, 0.1)
    async with db.connect() as conn:
        jobs = (await conn.execute(select(t.jobs))).mappings().all()
        assert next(r for r in jobs if r["key"] == "budget-wait")["due_at"] < now()
        assert next(r for r in jobs if r["key"] == "uncertain")["due_at"] == future
    await Maintenance(
        db,
        bot,
        Settings(
            _env_file=None, db_url="postgresql://unused", global_daily_budget_usd=10
        ),
    ).tick()
    async with db.connect() as conn:
        alerts = (
            (await conn.execute(select(t.outbox).where(t.outbox.c.kind == "budget")))
            .mappings()
            .all()
        )
    assert len(alerts) == 1
    assert alerts[0]["destination"] == 111 and "$0.1000" in alerts[0]["payload"]["text"]
