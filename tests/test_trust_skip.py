from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.v2 import tables as t
from app.v2.domain import now
from app.v2.pipeline import Pipeline
from app.v2.presentation import label
from app.v2.state import ensure_member, ensure_user
from conftest import message
from test_pipeline import Classifier, OCR, check_all, counts


async def grant(db, *, source="manual", chat_id=1, **extra):
    async with db.begin() as conn:
        uid = await ensure_user(conn, 900)
        await ensure_member(conn, chat_id, uid)
        await conn.execute(
            t.trust.insert().values(
                chat_id=chat_id, user_id=uid, source=source, reason="Test", **extra
            )
        )
    return uid


@pytest.mark.parametrize("source", ["manual", "auto"])
@pytest.mark.parametrize("photo", [False, True])
async def test_trusted_publications_and_edits_skip_all_external_checks(
    db, bot, source, photo
):
    await grant(db, source=source)
    bot.get_chat_member = AsyncMock(side_effect=RuntimeError("Telegram unavailable"))
    classifier = Classifier("SCAM")
    ocr = OCR()
    ocr.extract = AsyncMock(side_effect=RuntimeError("Must not run"))
    pipeline = Pipeline(db, bot, classifier, ocr)
    for revision in (1, 2):
        kwargs = (
            {
                "text": None,
                "caption": f"Photo revision {revision}",
                "photo": [
                    {
                        "file_id": "photo",
                        "file_unique_id": "unique",
                        "width": 20,
                        "height": 20,
                    }
                ],
            }
            if photo
            else {"text": f"Text revision {revision}"}
        )
        await pipeline.ingest(message(**kwargs), revision)
        await check_all(db, pipeline)
    bot.get_chat_member.assert_not_awaited()
    ocr.extract.assert_not_awaited()
    assert not classifier.calls
    assert not bot.calls
    for table in (t.actions, t.outbox, t.strikes, t.credits):
        assert await counts(db, table) == 0
    async with db.connect() as conn:
        checks = (await conn.execute(select(t.checks))).mappings().all()
        assert len(checks) == 2
        assert all(
            c["status"] == "skipped" and c["reason"] == "trusted" and c["error"] is None
            for c in checks
        )
        saved = (await conn.execute(select(t.trust))).mappings().one()
        assert saved["revoked_at"] is None and saved["source"] == source


@pytest.mark.parametrize("state", ["expired", "revoked", "other_group", "absent"])
async def test_only_active_local_trust_bypasses_checks(db, bot, state):
    if state != "absent":
        await grant(
            db,
            chat_id=2 if state == "other_group" else 1,
            **(
                {"expires_at": now() - timedelta(seconds=1)}
                if state == "expired"
                else {}
            ),
            **({"revoked_at": now()} if state == "revoked" else {}),
        )
    classifier = Classifier("SCAM")
    pipeline = Pipeline(db, bot, classifier, OCR())
    await pipeline.ingest(message(), 1)
    await check_all(db, pipeline)
    assert len(classifier.calls) == 1
    assert ("get_chat_member", -1001, 900) in bot.calls
    assert await counts(db, t.strikes) == 1
    assert await counts(db, t.outbox) > 0


async def test_explicit_blacklist_keeps_precedence_over_trust(db, bot):
    uid = await grant(db)
    async with db.begin() as conn:
        await conn.execute(
            t.deny.insert().values(chat_id=1, user_id=uid, reason="Explicit deny")
        )
    classifier = Classifier()
    pipeline = Pipeline(db, bot, classifier, OCR())
    await pipeline.ingest(message(), 1)
    await check_all(db, pipeline)
    assert not classifier.calls
    async with db.connect() as conn:
        assert (
            await conn.execute(select(t.checks.c.reason))
        ).scalar_one() == "blacklisted"
        assert (await conn.execute(select(t.actions.c.kind))).scalar_one() == "delete"


async def test_untrusted_membership_failure_still_requests_review(db, bot):
    bot.get_chat_member = AsyncMock(side_effect=RuntimeError("Telegram unavailable"))
    classifier = Classifier()
    pipeline = Pipeline(db, bot, classifier, OCR())
    await pipeline.ingest(message(), 1)
    await check_all(db, pipeline)
    assert not classifier.calls
    assert await counts(db, t.actions) == 0
    assert await counts(db, t.outbox) == 1
    async with db.connect() as conn:
        row = (await conn.execute(select(t.checks))).mappings().one()
        assert row["status"] == "error" and row["error"] == "membership_unavailable"
    assert "Telegram" in label("membership_unavailable")
    assert "membership_unavailable" not in label("membership_unavailable")
