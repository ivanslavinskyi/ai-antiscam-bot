import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, func

from app.v2 import tables as t
from app.v2.actions import ActionExecutor
from app.v2.domain import Denied, now
from app.v2.operator import Operator
from app.v2.pipeline import Pipeline
from app.v2.queue import Queue, enqueue
from app.v2.state import audit, ensure_user
from app.v2.maintenance import Maintenance
from app.v2.config import Settings
from app.v2.delivery import Delivery
from app.v2.onboarding import Onboarding
from conftest import message
from test_pipeline import Classifier, OCR, check_all, counts


async def test_cancelled_confirmation_cannot_later_apply(db, bot, auth, scope):
    pipeline = Pipeline(db, bot, Classifier(), OCR())
    await pipeline.ingest(message(), 1)
    operator = Operator(db, auth)
    profile = await operator.profile(scope, 1)
    token = await operator.issue(scope, "trust_add", {"user_id": 1}, profile["version"])
    with pytest.raises(Denied):
        await operator.cancel(333, -2001, token)
    cancelled = await operator.cancel(222, -2001, token)
    assert await operator.confirm(222, -2001, token) == cancelled
    assert await counts(db, t.trust) == 0


async def test_manager_cannot_grant_or_remove_manager(db, auth, scope):
    operator = Operator(db, auth)
    for payload in [
        {"actor_id": 555, "role": "manager"},
        {"actor_id": 222, "role": "remove"},
    ]:
        token = await operator.issue(scope, "role", payload)
        with pytest.raises(Denied):
            await operator.confirm(222, -2001, token)
    owner_scope = await auth.scope(111, -2001, 1)
    token = await operator.issue(
        owner_scope, "role", {"actor_id": 555, "role": "manager"}
    )
    await operator.confirm(111, -2001, token)
    async with db.connect() as conn:
        assert (
            await conn.execute(
                select(t.roles.c.role).where(
                    t.roles.c.actor_id == 555, t.roles.c.revoked_at.is_(None)
                )
            )
        ).scalar_one() == "manager"


async def test_concurrent_safe_messages_grant_trust_once(db, bot):
    pipeline = Pipeline(db, bot, Classifier(), OCR())
    for i in range(1, 7):
        await pipeline.ingest(message(i), i)
    async with db.connect() as conn:
        jobs = [
            dict(r, attempt=1)
            for r in (
                await conn.execute(select(t.jobs).where(t.jobs.c.kind == "check"))
            ).mappings()
        ]
    await asyncio.gather(*(pipeline.check(job) for job in jobs))
    await asyncio.gather(*(pipeline.check(job) for job in jobs))
    assert await counts(db, t.trust) == 1
    async with db.connect() as conn:
        assert (
            await conn.execute(
                select(func.count())
                .select_from(t.credits)
                .where(t.credits.c.eligible.is_(True))
            )
        ).scalar_one() >= 5
        assert (
            await conn.execute(
                select(func.count())
                .select_from(t.outbox)
                .where(t.outbox.c.kind == "trust_notice")
            )
        ).scalar_one() == 1


async def test_hot_group_checks_overlap_but_effects_stay_serial(db):
    async with db.begin() as conn:
        for i in range(2):
            await enqueue(conn, "check", f"check-{i}", {}, 1)
    queue = Queue(db)
    first, second = await queue.claim(False), await queue.claim(False)
    assert first and second and first["id"] != second["id"]
    async with db.begin() as conn:
        for i in range(2):
            await enqueue(conn, "action", f"action-{i}", {}, 1, priority=1)
    assert (await queue.claim(False))["kind"] == "action"
    assert await queue.claim(False) is None


@pytest.mark.parametrize("provenance", ["bot", "human", "missing"])
async def test_not_scam_preserves_bans_until_explicit_unban(
    db, bot, auth, scope, provenance
):
    pipeline = Pipeline(db, bot, Classifier("SCAM"), OCR())
    for i in range(1, 6):
        await pipeline.ingest(message(i, text="SCAM offer"), i)
    await check_all(db, pipeline)
    async with db.begin() as conn:
        ban = dict(
            (await conn.execute(select(t.actions).where(t.actions.c.kind == "ban")))
            .mappings()
            .one()
        )
        await conn.execute(
            t.actions.update()
            .where(t.actions.c.id == ban["id"])
            .values(state="succeeded", before={"status": "member"}, result={})
        )
        # Other planned restrictions are cancelled to isolate reversal provenance.
        await conn.execute(
            t.actions.update()
            .where(t.actions.c.state == "planned")
            .values(state="cancelled")
        )
        if provenance != "missing":
            await audit(
                conn,
                1,
                bot.id if provenance == "bot" else 444,
                "restriction.observed",
                user_id=900,
                status="kicked",
            )
        incident = dict(
            (
                await conn.execute(
                    select(t.incidents).where(t.incidents.c.id == ban["incident_id"])
                )
            )
            .mappings()
            .one()
        )
    bot.members[(-1001, 900)] = "kicked"
    operator = Operator(db, auth)
    token = await operator.issue(
        scope,
        "label",
        {"id": str(incident["id"]), "label": "NOT_SCAM", "enforce": False},
        incident["version"],
    )
    await operator.confirm(222, -2001, token)
    assert await counts(db, t.actions) > 0
    async with db.connect() as conn:
        assert not (
            await conn.execute(
                select(t.actions.c.id).where(t.actions.c.kind == "unban")
            )
        ).first()
    assert bot.members[(-1001, 900)] == "kicked"
    profile = await operator.profile(scope, incident["user_id"])
    token = await operator.issue(
        scope, "unban", {"user_id": incident["user_id"]}, profile["version"]
    )
    await operator.confirm(222, -2001, token)
    async with db.connect() as conn:
        reversal = dict(
            (await conn.execute(select(t.actions).where(t.actions.c.kind == "unban")))
            .mappings()
            .one()
        )
        job = dict(
            (
                await conn.execute(
                    select(t.jobs).where(
                        t.jobs.c.payload["id"].as_string() == str(reversal["id"]),
                        t.jobs.c.kind == "action",
                    )
                )
            )
            .mappings()
            .one()
        )
    await ActionExecutor(db, bot, auth).execute(job)
    calls = [c for c in bot.calls if c[0] == "unban_chat_member"]
    assert calls  # Explicit moderator decision, unlike automatic compensation.
    if calls:
        assert calls[0][3]["only_if_banned"] is True


async def test_history_export_uses_filters_and_group_scope(db, bot, auth, scope):
    pipeline = Pipeline(db, bot, Classifier(), OCR())
    for gid in [1, 2]:
        await pipeline.ingest(message(1, gid=gid), gid)
    await check_all(db, pipeline)
    async with db.begin() as conn:
        await conn.execute(
            t.incidents.update()
            .where(t.incidents.c.chat_id == 1)
            .values(created_at=now() - timedelta(days=2))
        )
    operator = Operator(db, auth)
    recent = (await operator.export(scope, "history", {"days": 1})).decode("utf-8-sig")
    all_rows = (await operator.export(scope, "history")).decode("utf-8-sig")
    assert len(recent.splitlines()) == 1
    assert len(all_rows.splitlines()) == 2


async def test_existing_cards_are_updated_after_moderator_decision(
    db, bot, auth, scope
):
    pipeline = Pipeline(db, bot, Classifier("SCAM"), OCR())
    await pipeline.ingest(message(), 1)
    await check_all(db, pipeline)
    delivery = Delivery(db, bot, Onboarding(db, bot, auth))
    async with db.connect() as conn:
        job = dict(
            (await conn.execute(select(t.jobs).where(t.jobs.c.kind == "outbox")))
            .mappings()
            .one()
        )
        incident = dict((await conn.execute(select(t.incidents))).mappings().one())
    await delivery.execute(job)
    operator = Operator(db, auth)
    token = await operator.issue(
        scope,
        "label",
        {"id": str(incident["id"]), "label": "NOT_SCAM", "enforce": False},
        incident["version"],
    )
    await operator.confirm(222, -2001, token)
    async with db.connect() as conn:
        jobs = [
            dict(r)
            for r in (
                await conn.execute(select(t.jobs).where(t.jobs.c.kind == "outbox"))
            ).mappings()
        ]
    for item in jobs:
        await delivery.execute(item)
    assert len([c for c in bot.calls if c[0] == "send_message"]) == 1
    assert any(
        "Решение модератора: Не скам" in c[1]
        for c in bot.calls
        if c[0] == "edit_message_text"
    )


async def test_expiration_starts_new_cycle_and_counts_first_new_message(
    db, bot, auth, scope
):
    pipeline = Pipeline(db, bot, Classifier(), OCR())
    for i in range(1, 6):
        await pipeline.ingest(message(i), i)
    await check_all(db, pipeline)
    async with db.begin() as conn:
        await conn.execute(t.trust.update().values(expires_at=now()))
    await pipeline.ingest(message(6), 6)
    await check_all(db, pipeline)
    operator = Operator(db, auth)
    profile = await operator.profile(scope, 1)
    assert profile["cycle"] == 1 and profile["credits"] == 1 and not profile["trust"]
    await pipeline.ingest(message(1, text="Edited old publication"), 7)
    await check_all(db, pipeline)
    assert (await operator.profile(scope, 1))["credits"] == 1


async def test_observed_identity_refresh_preserves_legacy_flags(db):
    async with db.begin() as conn:
        uid = await ensure_user(conn, 777, username="old_name")
        await conn.execute(
            t.users.update()
            .where(t.users.c.id == uid)
            .values(is_global_whitelisted=True)
        )
        assert await ensure_user(conn, 777, username="new_name") == uid
        row = (
            (await conn.execute(select(t.users).where(t.users.c.id == uid)))
            .mappings()
            .one()
        )
        assert row["username"] == "new_name" and row["is_global_whitelisted"]


async def test_raw_retention_keeps_unprocessed_data_and_receipts(db, bot):
    async with db.begin() as conn:
        for i, state in enumerate(["done", "pending"]):
            await conn.execute(
                t.inbox.insert().values(
                    bot_id=bot.id,
                    update_id=100 + i,
                    state=state,
                    payload={"fixture": "raw"},
                    created_at=now() - timedelta(days=8),
                )
            )
    await Maintenance(
        db, bot, Settings(_env_file=None, db_url="postgresql://unused")
    ).tick()
    async with db.connect() as conn:
        rows = (
            await conn.execute(
                select(t.inbox.c.state, t.inbox.c.payload).order_by(t.inbox.c.update_id)
            )
        ).all()
        assert rows == [("done", {}), ("pending", {"fixture": "raw"})]


async def test_backlog_warning_escalates_without_waiting_for_sanction_deadline(db, bot):
    from app.v2.queue import enqueue

    maintenance = Maintenance(
        db, bot, Settings(_env_file=None, db_url="postgresql://unused")
    )
    async with db.begin() as conn:
        await enqueue(
            conn,
            "check",
            "backlog-fixture",
            {},
            1,
            due_at=now() - timedelta(seconds=65),
        )
    await maintenance.tick()
    await maintenance.tick()
    async with db.begin() as conn:
        warnings = (
            (
                await conn.execute(
                    select(t.outbox).where(t.outbox.c.key.like("backlog:warning:%"))
                )
            )
            .mappings()
            .all()
        )
        assert len(warnings) == 1 and warnings[0]["destination"] == 111
        await conn.execute(
            t.jobs.update()
            .where(t.jobs.c.key == "backlog-fixture")
            .values(due_at=now() - timedelta(seconds=305))
        )
    await maintenance.tick()
    async with db.connect() as conn:
        critical = (
            (
                await conn.execute(
                    select(t.outbox).where(t.outbox.c.key.like("backlog:critical:%"))
                )
            )
            .mappings()
            .all()
        )
        assert len(critical) == 1 and critical[0]["destination"] == 111
