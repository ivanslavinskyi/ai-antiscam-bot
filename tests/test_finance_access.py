from datetime import timedelta

import pytest
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.v2 import finance, tables as t
from app.v2.config import Settings
from app.v2.domain import Denied, Conflict, Policy, now
from app.v2.delivery import Delivery
from app.v2.maintenance import Maintenance
from app.v2.onboarding import Onboarding
from app.v2.operator import Operator
from app.v2.queue import emit
from app.v2.ui import UI, SETTING_SECTIONS
from conftest import message


async def test_finance_owner_private_only_and_confirmation_is_idempotent(db):
    for actor, dest in [(222, 222), (222, -2001), (111, -2001)]:
        with pytest.raises(Denied):
            await finance.overview(db, actor, dest)
        with pytest.raises(Denied):
            await finance.prepare(db, actor, dest, 1, 0.5)
    token = await finance.prepare(db, 111, 111, 1, 0.5)
    with pytest.raises(Denied):
        await finance.confirm(db, 222, 222, token)
    with pytest.raises(Denied):
        await finance.confirm(db, 111, -2001, token)
    result = await finance.confirm(db, 111, 111, token)
    assert await finance.confirm(db, 111, 111, token) == result
    async with db.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(t.group_settings).order_by(t.group_settings.c.chat_id)
                )
            )
            .mappings()
            .all()
        )
    assert rows[0]["policy"]["daily_budget_usd"] == 0.5
    assert rows[0]["version"] == 2
    assert rows[1]["policy"]["daily_budget_usd"] is None
    assert rows[1]["version"] == 1


async def test_budget_role_rechecked_at_confirmation(db):
    token = await finance.prepare(db, 111, 111, 1, 0.5)
    async with db.begin() as conn:
        await conn.execute(
            t.roles.update().where(t.roles.c.actor_id == 111).values(revoked_at=now())
        )
    with pytest.raises(Denied):
        await finance.confirm(db, 111, 111, token)


async def test_budget_rejects_stale_policy_and_expired_token(db):
    token = await finance.prepare(db, 111, 111, 1, 0.5)
    async with db.begin() as conn:
        await conn.execute(
            t.group_settings.update()
            .where(t.group_settings.c.chat_id == 1)
            .values(version=2)
        )
    with pytest.raises(Conflict):
        await finance.confirm(db, 111, 111, token)
    token = await finance.prepare(db, 111, 111, 1, 0.5)
    async with db.begin() as conn:
        await conn.execute(
            t.operations.update()
            .where(t.operations.c.token == token)
            .values(expires_at=now() - timedelta(seconds=1))
        )
    with pytest.raises(Conflict):
        await finance.confirm(db, 111, 111, token)


async def test_legacy_admin_budget_write_and_existing_confirmation_blocked(
    db, auth, scope
):
    op = Operator(db, auth)
    with pytest.raises(Denied):
        await op.issue(scope, "settings", {"changes": {"daily_budget_usd": 1}})
    async with db.begin() as conn:
        await conn.execute(
            t.operations.insert().values(
                token="old-budget",
                actor_id=222,
                admin_chat_id=-2001,
                chat_id=1,
                binding_version=1,
                kind="settings",
                expires_at=now() + timedelta(minutes=2),
                payload={"changes": {"daily_budget_usd": 1}, "policy_version": 1},
            )
        )
    with pytest.raises(Denied):
        await op.confirm(222, -2001, "old-budget")


async def test_shared_settings_and_stats_do_not_expose_money(db, bot, auth, scope):
    ui = UI(db, bot, auth, Operator(db, auth), Onboarding(db, bot, auth))
    await ui.settings(scope)
    for section in SETTING_SECTIONS:
        await ui.settings(scope, section=section)
    await ui.stats(scope)
    for call in bot.calls:
        if call[0] != "send_message":
            continue
        assert "$" not in call[2] and "USD" not in call[2]
        kb = call[3].get("reply_markup")
        assert not any(
            "daily_budget_usd" in (b.callback_data or "")
            for row in kb.inline_keyboard
            for b in row
        )
    msg = message(uid=222).model_copy(
        update={"chat": message().chat.model_copy(update={"id": -2001})}
    )
    query = CallbackQuery(
        id="budget",
        from_user=msg.from_user,
        chat_instance="test",
        message=msg,
        data="v:setting:1:daily_budget_usd",
    )
    await ui.callback(query)
    assert "личной панели" in [c for c in bot.calls if c[0] == "send_message"][-1][2]


async def test_budget_alerts_only_go_to_owner_and_old_shared_alert_is_cancelled(
    db, bot, auth
):
    async with db.begin() as conn:
        await conn.execute(
            t.group_settings.update()
            .where(t.group_settings.c.chat_id == 1)
            .values(policy=Policy(daily_budget_usd=1).model_dump())
        )
        await conn.execute(
            t.budget.insert().values(
                chat_id=1, day=now().date().isoformat(), key="used", reserved=0.9
            )
        )
        oid = await emit(
            conn,
            destination=-2001,
            kind="budget",
            payload={"text": "$0.90"},
            key="old-shared",
        )
    await Maintenance(
        db,
        bot,
        Settings(
            _env_file=None, db_url="postgresql://unused", global_daily_budget_usd=1
        ),
    ).tick()
    async with db.connect() as conn:
        alerts = (
            (
                await conn.execute(
                    select(t.outbox).where(
                        t.outbox.c.kind == "budget", t.outbox.c.id != oid
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(alerts) == 2
    assert all(r["destination"] == 111 and r["chat_id"] is None for r in alerts)
    delivery = Delivery(db, bot, Onboarding(db, bot, auth))
    await delivery.execute({"payload": {"id": str(oid)}})
    async with db.begin() as conn:
        assert (
            await conn.execute(select(t.outbox.c.state).where(t.outbox.c.id == oid))
        ).scalar_one() == "cancelled"
        await conn.execute(
            t.roles.update().where(t.roles.c.actor_id == 111).values(revoked_at=now())
        )
    await delivery.execute({"payload": {"id": str(alerts[0]["id"])}})
    assert not [c for c in bot.calls if c[0] == "send_message"]


async def test_owner_budget_full_private_ui_flow(db, bot, auth):
    ui = UI(db, bot, auth, Operator(db, auth), Onboarding(db, bot, auth))
    msg = message(uid=111, text="0.75").model_copy(
        update={
            "chat": message().chat.model_copy(update={"id": 111, "type": "private"})
        }
    )
    query = CallbackQuery(
        id="private-budget",
        from_user=msg.from_user,
        chat_instance="test",
        message=msg,
        data="v:budgetedit:1",
    )
    await ui.callback(query)
    await ui.message(msg)
    async with db.connect() as conn:
        token = (
            await conn.execute(
                select(t.operations.c.token).where(
                    t.operations.c.kind == "owner_budget"
                )
            )
        ).scalar_one()
    await ui.callback(query.model_copy(update={"data": f"v:budgetconfirm:{token}"}))
    async with db.connect() as conn:
        policy = (
            await conn.execute(
                select(t.group_settings.c.policy).where(t.group_settings.c.chat_id == 1)
            )
        ).scalar_one()
    assert policy["daily_budget_usd"] == 0.75
    assert all(c[1] == 111 for c in bot.calls if c[0] == "send_message")
