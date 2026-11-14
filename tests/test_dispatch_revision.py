"""A new review must fence off sanctions from the previous incident revision."""

import pytest
from sqlalchemy import select

from app.v2 import tables as t
from app.v2.actions import ActionExecutor
from app.v2.operator import Operator
from app.v2.pipeline import Pipeline
from conftest import message
from test_pipeline import Classifier, OCR, check_all


@pytest.mark.parametrize("during_permissions", [False, True])
async def test_recheck_cancels_old_sanctions(db, bot, auth, scope, during_permissions):
    pipeline = Pipeline(db, bot, Classifier("SCAM"), OCR())
    await pipeline.ingest(message(text="SCAM offer"), 1)
    await check_all(db, pipeline)
    async with db.connect() as conn:
        incident = (await conn.execute(select(t.incidents))).mappings().one()
        action = (
            (await conn.execute(select(t.actions).where(t.actions.c.kind == "delete")))
            .mappings()
            .one()
        )
    operator = Operator(db, auth)
    token = await operator.issue(
        scope, "retry_check", {"id": str(incident["id"])}, incident["version"]
    )
    original = bot.get_chat_member
    requested = False

    async def recheck():
        nonlocal requested
        requested = True
        await operator.confirm(scope.actor_id, scope.admin_chat_id, token)
        async with db.connect() as conn:
            assert not (
                await conn.execute(
                    select(t.actions.c.id).where(t.actions.c.state == "planned")
                )
            ).first()

    async def get_member(chat, user):
        if not requested and user == bot.id:
            await recheck()
        return await original(chat, user)

    if during_permissions:
        bot.get_chat_member = get_member
    else:
        await recheck()
    await ActionExecutor(db, bot, auth).execute({"payload": {"id": str(action["id"])}})
    assert requested
    assert not any(call[0] == "delete_message" for call in bot.calls)
    async with db.connect() as conn:
        assert (
            await conn.execute(select(t.incidents.c.version))
        ).scalar_one() == incident["version"] + 1


async def test_dispatch_rechecks_revision_after_telegram_reads(db, bot, auth):
    pipeline = Pipeline(db, bot, Classifier("SCAM"), OCR())
    await pipeline.ingest(message(text="SCAM offer"), 1)
    await check_all(db, pipeline)
    async with db.connect() as conn:
        action = (
            (await conn.execute(select(t.actions).where(t.actions.c.kind == "delete")))
            .mappings()
            .one()
        )
    original = bot.get_chat_member
    revised = False

    async def get_member(chat, user):
        nonlocal revised
        if not revised:
            revised = True
            async with db.begin() as conn:
                await conn.execute(
                    t.incidents.update().values(version=t.incidents.c.version + 1)
                )
        return await original(chat, user)

    bot.get_chat_member = get_member
    await ActionExecutor(db, bot, auth).execute({"payload": {"id": str(action["id"])}})
    assert not any(call[0] == "delete_message" for call in bot.calls)
    async with db.connect() as conn:
        row = (
            (
                await conn.execute(
                    select(t.actions).where(t.actions.c.id == action["id"])
                )
            )
            .mappings()
            .one()
        )
        assert (row["state"], row["error"]) == ("cancelled", "revision_changed")
