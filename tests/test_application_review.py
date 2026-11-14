from datetime import timedelta

import pytest
from sqlalchemy import select, func

from app.v2 import tables as t
from app.v2.domain import Conflict, Denied, now
from app.v2.delivery import Delivery
from app.v2.onboarding import Onboarding
from app.v2.operator import Operator
from app.v2.pipeline import Pipeline
from conftest import message
from test_pipeline import Classifier, OCR


async def prepare(wizard, bot, actor, work=-3001, admin=-3002):
    bot.members[(work, actor)] = "administrator"
    bot.members[(admin, actor)] = "administrator"
    rid = await wizard.create(actor)
    for side, chat in (("work", work), ("admin", admin)):
        selection, _ = await wizard.selection(actor, rid, side)
        await wizard.share(actor, selection, chat)
    return await wizard.get(actor, rid)


async def test_applicant_cannot_connect_without_owner_and_wait_is_durable(
    db, bot, auth
):
    wizard = Onboarding(db, bot, auth)
    row = await prepare(wizard, bot, 444)
    await wizard.submit(444, row["id"], row["version"])
    async with db.begin() as conn:
        await conn.execute(
            t.onboarding.update()
            .where(t.onboarding.c.id == row["id"])
            .values(expires_at=now() - timedelta(days=2))
        )
        assert (
            await conn.execute(select(func.count()).select_from(t.chats))
        ).scalar_one() == 2
    assert (await wizard.get(444, row["id"]))["state"] == "submitted"
    with pytest.raises(Conflict):
        await wizard.confirm(444, row["id"], row["version"])
    with pytest.raises(Denied):
        await wizard.get(333, row["id"])
    gid = await wizard.confirm(111, row["id"], row["version"])
    async with db.connect() as conn:
        outbox = dict(
            (
                await conn.execute(
                    select(t.outbox).where(t.outbox.c.kind == "onboarding")
                )
            )
            .mappings()
            .one()
        )
        assert (
            await conn.execute(
                select(t.group_settings.c.lifecycle).where(
                    t.group_settings.c.chat_id == gid
                )
            )
        ).scalar_one() == "pending"
    await Delivery(db, bot, wizard).execute({"payload": {"id": str(outbox["id"])}})
    async with db.connect() as conn:
        settings = (
            (
                await conn.execute(
                    select(t.group_settings).where(t.group_settings.c.chat_id == gid)
                )
            )
            .mappings()
            .one()
        )
        assert settings["mode"] == "observe" and settings["lifecycle"] == "connected"
        assert (
            await conn.execute(
                select(t.bindings.c.admin_chat_id).where(
                    t.bindings.c.chat_id == 1, t.bindings.c.closed_at.is_(None)
                )
            )
        ).scalar_one() == -2001


async def test_returned_application_needs_new_version_and_rights(db, bot, auth):
    wizard = Onboarding(db, bot, auth)
    row = await prepare(wizard, bot, 444)
    await wizard.submit(444, row["id"], row["version"])
    await wizard.review(111, row["id"], row["version"], "revise", "Проверьте настройки")
    new = await wizard.get(444, row["id"])
    assert new["version"] > row["version"] and new["state"] == "pending"
    await wizard.submit(444, row["id"], new["version"])
    with pytest.raises(Conflict):
        await wizard.confirm(111, row["id"], row["version"])
    bot.members[(-3001, 444)] = "member"
    with pytest.raises(Denied):
        await wizard.confirm(111, row["id"], new["version"])
    async with db.connect() as conn:
        assert (
            await conn.execute(select(func.count()).select_from(t.chats))
        ).scalar_one() == 2


async def test_applicant_cannot_hijack_existing_pair(db, bot, auth):
    wizard = Onboarding(db, bot, auth)
    row = await prepare(wizard, bot, 444, work=-1001)
    with pytest.raises(Conflict):
        await wizard.submit(444, row["id"], row["version"])
    async with db.connect() as conn:
        assert (
            await conn.execute(
                select(t.bindings.c.admin_chat_id).where(
                    t.bindings.c.chat_id == 1, t.bindings.c.closed_at.is_(None)
                )
            )
        ).scalar_one() == -2001


async def test_transfer_keeps_old_pair_and_trust_until_success(db, bot, auth, scope):
    pipeline = Pipeline(db, bot, Classifier(), OCR())
    await pipeline.ingest(message(), 1)
    op = Operator(db, auth)
    profile = await op.profile(scope, 1)
    token = await op.issue(scope, "trust_add", {"user_id": 1}, profile["version"])
    await op.confirm(222, -2001, token)
    wizard = Onboarding(db, bot, auth)
    row = await prepare(wizard, bot, 111, work=-1001, admin=-3002)
    gid = await wizard.confirm(111, row["id"], row["version"])
    assert gid == 1
    async with db.connect() as conn:
        notice = dict(
            (
                await conn.execute(
                    select(t.outbox).where(t.outbox.c.kind == "onboarding_transfer")
                )
            )
            .mappings()
            .one()
        )
        assert (
            await conn.execute(
                select(t.bindings.c.admin_chat_id).where(
                    t.bindings.c.chat_id == 1, t.bindings.c.closed_at.is_(None)
                )
            )
        ).scalar_one() == -2001
        assert (
            await conn.execute(
                select(t.roles.c.id).where(
                    t.roles.c.chat_id == 1,
                    t.roles.c.actor_id == 222,
                    t.roles.c.revoked_at.is_(None),
                )
            )
        ).first()
    await Delivery(db, bot, wizard).execute({"payload": {"id": str(notice["id"])}})
    async with db.connect() as conn:
        assert (
            await conn.execute(
                select(t.bindings.c.admin_chat_id).where(
                    t.bindings.c.chat_id == 1, t.bindings.c.closed_at.is_(None)
                )
            )
        ).scalar_one() == -3002
        assert (
            await conn.execute(
                select(t.trust.c.id).where(
                    t.trust.c.chat_id == 1, t.trust.c.revoked_at.is_(None)
                )
            )
        ).first()
        assert (
            await conn.execute(
                select(t.group_settings.c.mode).where(t.group_settings.c.chat_id == 1)
            )
        ).scalar_one() == "observe"


async def test_transfer_failed_delivery_keeps_old_connection(db, bot, auth):
    wizard = Onboarding(db, bot, auth)
    row = await prepare(wizard, bot, 111, work=-1001, admin=-3002)
    await wizard.confirm(111, row["id"], row["version"])
    async with db.connect() as conn:
        notice = (
            (
                await conn.execute(
                    select(t.outbox).where(t.outbox.c.kind == "onboarding_transfer")
                )
            )
            .mappings()
            .one()
        )
    bot.fail_send = True
    await Delivery(db, bot, wizard).execute({"payload": {"id": str(notice["id"])}})
    async with db.connect() as conn:
        assert (
            await conn.execute(
                select(t.bindings.c.admin_chat_id).where(
                    t.bindings.c.chat_id == 1, t.bindings.c.closed_at.is_(None)
                )
            )
        ).scalar_one() == -2001
        assert (
            await conn.execute(
                select(t.outbox.c.state).where(t.outbox.c.id == notice["id"])
            )
        ).scalar_one() == "unknown"
