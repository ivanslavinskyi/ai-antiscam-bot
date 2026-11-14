import asyncio
from datetime import timedelta

import pytest
from aiogram.types import Update
from sqlalchemy import select, func

from app.v2 import tables as t
from app.v2.domain import Denied, now
from app.v2.llm import Budget, RetryLater
from app.v2.queue import Queue, persist_updates, enqueue
from app.v2.onboarding import Onboarding
from app.v2.delivery import Delivery
from app.v2.actions import ActionExecutor
from app.v2.pipeline import Pipeline
from app.v2.operator import Operator
from app.v2.state import ensure_user, lock_member, plan_action
from conftest import message
from test_pipeline import Classifier, OCR, check_all, counts


async def test_budget_reservation_is_atomic_across_groups(db):
    budget = Budget(db, 0.01)
    results = await asyncio.gather(
        budget.reserve(1, "a", 0.008, 0.1),
        budget.reserve(2, "b", 0.008, 0.1),
        return_exceptions=True,
    )
    assert sum(isinstance(v, RetryLater) for v in results) == 1
    assert await counts(db, t.budget) == 1


async def test_inbox_unique_and_cursor_never_goes_backward(db):
    updates = [Update(update_id=i, message=message(i)) for i in [10, 11]]
    await persist_updates(db, 999001, updates)
    await persist_updates(db, 999001, updates[:1])
    assert await counts(db, t.inbox) == 2
    async with db.connect() as conn:
        assert (await conn.execute(select(t.cursors.c.next_offset))).scalar_one() == 12
        assert (
            await conn.execute(select(func.count()).select_from(t.jobs))
        ).scalar_one() == 2


async def test_lease_recovery_cannot_be_completed_by_old_worker(db):
    async with db.begin() as conn:
        await enqueue(conn, "check", "first", {}, 1)
        await enqueue(conn, "check", "second", {}, 2)
    queue = Queue(db)
    first = await queue.claim()
    second = await queue.claim()
    assert first["chat_id"] != second["chat_id"]
    async with db.begin() as conn:
        await conn.execute(
            t.jobs.update()
            .where(t.jobs.c.id == first["id"])
            .values(lease_until=now() - timedelta(seconds=1))
        )
    replacement = await queue.claim()
    assert (
        replacement["id"] == first["id"]
        and replacement["lease_token"] != first["lease_token"]
    )
    await queue.finish(first)
    async with db.connect() as conn:
        assert (
            await conn.execute(select(t.jobs.c.state).where(t.jobs.c.id == first["id"]))
        ).scalar_one() == "running"


async def test_control_lane_ingests_edits_while_group_check_runs(db):
    async with db.begin() as conn:
        await enqueue(conn, "check", "slow", {}, 1)
    q = Queue(db)
    await q.claim(control=False)
    await persist_updates(db, 999001, [Update(update_id=5, edited_message=message())])
    control = await q.claim(control=True)
    assert control["kind"] == "update"


async def test_private_onboarding_only_posts_in_admin_group(db, bot, auth):
    wizard = Onboarding(db, bot, auth)
    rid = await wizard.create(111)
    work_request, kb = await wizard.selection(111, rid, "work")
    request = kb.keyboard[0][0].request_chat
    assert request.bot_is_member is None
    await wizard.share(111, work_request, -1003)
    admin_request, _ = await wizard.selection(111, rid, "admin")
    await wizard.share(111, admin_request, -2003)
    row = await wizard.get(111, rid)
    gid = await wizard.confirm(111, rid, row["version"])
    async with db.connect() as conn:
        assert (
            await conn.execute(
                select(t.group_settings.c.lifecycle).where(
                    t.group_settings.c.chat_id == gid
                )
            )
        ).scalar_one() == "pending"
        job = dict(
            (await conn.execute(select(t.jobs).where(t.jobs.c.kind == "outbox")))
            .mappings()
            .one()
        )
    await Delivery(db, bot, wizard).execute(job)
    async with db.connect() as conn:
        assert (
            await conn.execute(
                select(t.group_settings.c.lifecycle).where(
                    t.group_settings.c.chat_id == gid
                )
            )
        ).scalar_one() == "connected"
        assert (
            await conn.execute(
                select(func.count())
                .select_from(t.trust)
                .where(t.trust.c.chat_id == gid)
            )
        ).scalar_one() == 0
    posts = [call for call in bot.calls if call[0] == "send_message"]
    assert len(posts) == 1 and posts[0][1] == -2003


async def test_onboarding_rejects_wrong_actor_replay_and_expiration(db, bot, auth):
    wizard = Onboarding(db, bot, auth)
    rid = await wizard.create(111)
    request, _ = await wizard.selection(111, rid, "work")
    with pytest.raises(Denied):
        await wizard.share(222, request, -1003)
    await wizard.share(111, request, -1003)
    with pytest.raises(Denied):
        await wizard.share(111, request, -1004)
    request, _ = await wizard.selection(111, rid, "admin")
    async with db.begin() as conn:
        await conn.execute(
            t.selections.update()
            .where(t.selections.c.request_id == request)
            .values(expires_at=now() - timedelta(seconds=1))
        )
    with pytest.raises(Denied):
        await wizard.share(111, request, -2003)


async def test_personal_invitation_cannot_be_claimed_by_other_actor(db, bot, auth):
    wizard = Onboarding(db, bot, auth)
    rid = await wizard.create(111, work_actor=555)
    token = await wizard.invite(111, rid, "work")
    with pytest.raises(Denied):
        await wizard.accept(444, token)
    request, _ = await wizard.accept(555, token)
    assert request >= 0
    with pytest.raises(Denied):
        await wizard.accept(555, token)


async def test_uncertain_delivery_does_not_activate_or_resend(db, bot, auth):
    wizard = Onboarding(db, bot, auth)
    rid = await wizard.create(111)
    for side, chat in [("work", -1003), ("admin", -2003)]:
        request, _ = await wizard.selection(111, rid, side)
        await wizard.share(111, request, chat)
    row = await wizard.get(111, rid)
    gid = await wizard.confirm(111, rid, row["version"])
    async with db.connect() as conn:
        job = dict(
            (await conn.execute(select(t.jobs).where(t.jobs.c.kind == "outbox")))
            .mappings()
            .one()
        )
    bot.fail_send = True
    delivery = Delivery(db, bot, wizard)
    await delivery.execute(job)
    await delivery.execute(job)
    assert len([c for c in bot.calls if c[0] == "send_message"]) == 1
    async with db.connect() as conn:
        assert (
            await conn.execute(
                select(t.group_settings.c.lifecycle).where(
                    t.group_settings.c.chat_id == gid
                )
            )
        ).scalar_one() == "pending"


async def prepared_action(db, kind, actor=None):
    async with db.begin() as conn:
        uid = await ensure_user(conn, 900)
        await lock_member(conn, 1, uid)
        settings = (
            (
                await conn.execute(
                    select(t.group_settings).where(t.group_settings.c.chat_id == 1)
                )
            )
            .mappings()
            .one()
        )
        aid = await plan_action(
            conn,
            settings,
            kind,
            {"message_id": 1},
            "test-action",
            user_id=uid,
            actor=actor,
        )
    return {"payload": {"id": str(aid)}}


async def test_uncertain_deletion_is_not_replayed(db, bot, auth):
    job = await prepared_action(db, "delete")
    bot.fail_action = True
    executor = ActionExecutor(db, bot, auth)
    await executor.execute(job)
    await executor.execute(job)
    assert len([c for c in bot.calls if c[0] == "delete_message"]) == 1
    async with db.connect() as conn:
        assert (await conn.execute(select(t.actions.c.state))).scalar_one() == "unknown"


async def test_unban_always_only_if_banned(db, bot, auth):
    job = await prepared_action(db, "unban", 222)
    await ActionExecutor(db, bot, auth).execute(job)
    calls = [c for c in bot.calls if c[0] == "unban_chat_member"]
    assert len(calls) == 1 and calls[0][3]["only_if_banned"] is True


async def test_pause_cancels_prepared_automatic_action(db, bot, auth, scope):
    job = await prepared_action(db, "ban")
    operator = Operator(db, auth)
    token = await operator.issue(scope, "mode", {"mode": "paused"})
    await operator.confirm(222, -2001, token)
    await ActionExecutor(db, bot, auth).execute(job)
    assert not [c for c in bot.calls if c[0] == "ban_chat_member"]


async def test_removed_admin_member_loses_old_confirmation(db, bot, auth, scope):
    operator = Operator(db, auth)
    token = await operator.issue(scope, "mode", {"mode": "paused"})
    bot.members[(-2001, 222)] = "left"
    with pytest.raises(Denied):
        await operator.confirm(222, -2001, token)


async def test_sender_chat_never_creates_fake_user(db, bot):
    p = Pipeline(db, bot, Classifier("SCAM"), OCR())
    await p.ingest(
        message(sender_chat={"id": -500, "type": "channel", "title": "Publisher"}), 1
    )
    await check_all(db, p)
    assert await counts(db, t.users) == 0
    assert await counts(db, t.strikes) == 0


async def test_reduced_threshold_does_not_bulk_grant(db, bot, auth, scope):
    p = Pipeline(db, bot, Classifier(), OCR())
    for i in range(1, 5):
        await p.ingest(message(i), i)
    await check_all(db, p)
    operator = Operator(db, auth)
    token = await operator.issue(
        scope, "settings", {"changes": {"auto_whitelist_threshold": 3}}
    )
    await operator.confirm(222, -2001, token)
    assert await counts(db, t.trust) == 0
    await p.ingest(message(5), 5)
    await check_all(db, p)
    assert await counts(db, t.trust) == 1


async def test_admin_commands_and_form_replies_bypass_work_chat_backlog(db):
    admin_message = message(2, text="/as").model_copy(
        update={"chat": message().chat.model_copy(update={"id": -2001})}
    )
    form_reply = admin_message.model_copy(
        update={"message_id": 3, "text": "Причина ручной модерации"}
    )
    await persist_updates(
        db,
        999001,
        [
            Update(update_id=1, message=message(1)),
            Update(update_id=2, message=admin_message),
            Update(update_id=3, message=form_reply),
        ],
    )
    queue = Queue(db)
    for update_id in (2, 3, 1):
        job = await queue.claim(control=True)
        async with db.connect() as conn:
            actual = (
                await conn.execute(
                    select(t.inbox.c.update_id).where(
                        t.inbox.c.id == job["payload"]["id"]
                    )
                )
            ).scalar_one()
        assert actual == update_id
        await queue.finish(job)
