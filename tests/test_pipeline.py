import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select, func

from app.v2 import tables as t
from app.v2.pipeline import Pipeline
from app.v2.llm import Verdict, CheckError
from app.v2.operator import Operator
from app.v2.domain import Denied, Conflict
from conftest import message


class Classifier:
    settings = SimpleNamespace(openai_model="gpt-4o-mini")

    def __init__(self, label="OK", confidence=0.99):
        self.label, self.confidence, self.calls = label, confidence, []

    async def classify(self, *args):
        self.calls.append(args)
        return Verdict(
            label=self.label,
            category="other",
            confidence=self.confidence,
            reason="Result",
        ), {}


class OCR:
    async def extract(self, *args):
        raise CheckError("ocr_timeout")


async def check_all(db, pipeline):
    async with db.connect() as conn:
        jobs = [
            dict(r, attempt=1)
            for r in (
                await conn.execute(
                    select(t.jobs)
                    .where(t.jobs.c.kind == "check")
                    .order_by(t.jobs.c.created_at)
                )
            ).mappings()
        ]
    for job in jobs:
        await pipeline.check(job)


async def counts(db, table):
    async with db.connect() as conn:
        return (
            await conn.execute(select(func.count()).select_from(table))
        ).scalar_one()


async def test_five_credits_are_local_and_replay_safe(db, bot):
    classifier = Classifier()
    p = Pipeline(db, bot, classifier, OCR())
    for i in range(1, 5):
        await p.ingest(message(i), i)
    await check_all(db, p)
    assert await counts(db, t.trust) == 0
    await p.ingest(message(5), 5)
    await check_all(db, p)
    assert await counts(db, t.trust) == 1
    await p.ingest(message(5), 5)
    await check_all(db, p)
    assert await counts(db, t.credits) == 5
    await p.ingest(message(1, gid=2), 6)
    await check_all(db, p)
    assert len(classifier.calls) == 6
    async with db.connect() as conn:
        assert (await conn.execute(select(t.trust.c.chat_id))).scalar_one() == 1


async def test_remove_trust_old_edit_does_not_fill_new_cycle(db, bot, auth, scope):
    p = Pipeline(db, bot, Classifier(), OCR())
    for i in range(1, 6):
        await p.ingest(message(i), i)
    await check_all(db, p)
    op = Operator(db, auth)
    profile = await op.profile(scope, 1)
    token = await op.issue(scope, "trust_remove", {"user_id": 1}, profile["version"])
    await op.confirm(222, -2001, token)
    await p.ingest(message(1, text="Edited old message"), 10)
    await check_all(db, p)
    profile = await op.profile(scope, 1)
    assert profile["credits"] == 0 and not profile["trust"]
    for i in range(6, 11):
        await p.ingest(message(i), i + 10)
    await check_all(db, p)
    assert (await op.profile(scope, 1))["trust"]


async def test_album_and_edits_one_incident_one_strike(db, bot):
    p = Pipeline(db, bot, Classifier("SCAM"), OCR())
    await p.ingest(message(1, text="Offer", media_group_id="album"), 1)
    await p.ingest(message(2, text="More", media_group_id="album"), 2)
    await check_all(db, p)
    assert await counts(db, t.incidents) == 1
    assert await counts(db, t.strikes) == 1
    await p.ingest(message(2, text="Changed", media_group_id="album"), 3)
    await check_all(db, p)
    assert await counts(db, t.strikes) == 1
    async with db.connect() as conn:
        assert (await conn.execute(select(t.member_states.c.balance))).scalar_one() == 1


async def test_stale_model_result_does_not_sanction_edit(db, bot):
    class EditingClassifier(Classifier):
        async def classify(self, *args):
            await pipeline.ingest(message(1, text="Corrected publication"), 2)
            return await super().classify(*args)

    pipeline = Pipeline(db, bot, EditingClassifier("SCAM"), OCR())
    await pipeline.ingest(message(1), 1)
    await check_all(db, pipeline)
    assert await counts(db, t.strikes) == 0
    assert await counts(db, t.actions) == 0


@pytest.mark.parametrize(
    "mode,label,confidence,strikes",
    [("observe", "SCAM", 0.99, 0), ("auto", "SCAM", 0.2, 0), ("auto", "SCAM", 0.99, 1)],
)
async def test_modes_confidence_and_strikes(db, bot, mode, label, confidence, strikes):
    async with db.begin() as conn:
        await conn.execute(
            t.group_settings.update()
            .where(t.group_settings.c.chat_id == 1)
            .values(mode=mode)
        )
    p = Pipeline(db, bot, Classifier(label, confidence), OCR())
    await p.ingest(message(1), 1)
    await check_all(db, p)
    assert await counts(db, t.strikes) == strikes


async def test_failed_ocr_is_saved_and_never_ok(db, bot):
    p = Pipeline(db, bot, Classifier(), OCR())
    await p.ingest(
        message(
            1,
            text="",
            photo=[
                {
                    "file_id": "image",
                    "file_unique_id": "unique",
                    "width": 100,
                    "height": 100,
                }
            ],
        ),
        1,
    )
    await check_all(db, p)
    async with db.connect() as conn:
        inc = (await conn.execute(select(t.incidents))).mappings().one()
        assert inc["status"] == "error" and inc["model_label"] is None
    assert await counts(db, t.credits) == 0
    assert await counts(db, t.actions) == 0


async def test_foreign_group_scope_and_role_revocation(db, bot, auth, scope):
    p = Pipeline(db, bot, Classifier("SCAM"), OCR())
    await p.ingest(message(1, gid=2), 1)
    await check_all(db, p)
    op = Operator(db, auth)
    async with db.connect() as conn:
        iid = (await conn.execute(select(t.incidents.c.id))).scalar_one()
    with pytest.raises(Denied):
        await op.detail(scope, iid)
    with pytest.raises(Denied):
        await auth.scope(222, -2002, 2)
    with pytest.raises(Denied):
        await auth.scope(444, -2001, 1)
    token = await op.issue(scope, "mode", {"mode": "paused"})
    async with db.begin() as conn:
        await conn.execute(
            t.roles.update()
            .where(t.roles.c.actor_id == 222)
            .values(revoked_at=func.now())
        )
    with pytest.raises(Denied):
        await op.confirm(222, -2001, token)


async def test_concurrent_decisions_and_replayed_confirmation(db, bot, auth, scope):
    p = Pipeline(db, bot, Classifier("SCAM"), OCR())
    await p.ingest(message(1), 1)
    await check_all(db, p)
    async with db.connect() as conn:
        inc = (await conn.execute(select(t.incidents))).mappings().one()
    op = Operator(db, auth)
    tokens = [
        await op.issue(
            scope, "label", {"id": str(inc["id"]), "label": "NOT_SCAM"}, inc["version"]
        )
        for _ in range(2)
    ]
    results = await asyncio.gather(
        *(op.confirm(222, -2001, token) for token in tokens), return_exceptions=True
    )
    assert sum(isinstance(r, Conflict) for r in results) == 1
    successful = tokens[0] if not isinstance(results[0], Exception) else tokens[1]
    await op.confirm(222, -2001, successful)
    async with db.connect() as conn:
        assert (await conn.execute(select(t.member_states.c.balance))).scalar_one() == 0
    assert await counts(db, t.decisions) == 1


async def test_archived_group_does_not_ingest(db, bot):
    async with db.begin() as conn:
        await conn.execute(
            t.group_settings.update()
            .where(t.group_settings.c.chat_id == 2)
            .values(lifecycle="archived")
        )
    p = Pipeline(db, bot, Classifier(), OCR())
    await p.ingest(message(1, gid=2), 1)
    assert await counts(db, t.items) == 0
