from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.v2 import tables as t
from app.v2.content import extract
from app.v2.card_view import incident_text
from app.v2.delivery import Delivery
from app.v2.onboarding import Onboarding
from app.v2.pipeline import Pipeline
from conftest import message
from test_design_atlas import ui_for
from test_pipeline import Classifier, OCR, check_all, counts
from test_trust_skip import grant


def external(chat_id=-9999, mid=77):
    return {
        "origin": {
            "type": "channel",
            "date": 1794900000,
            "chat": {"id": chat_id, "type": "channel", "title": "Bank claim"},
            "message_id": mid,
        },
        "chat": {"id": chat_id, "type": "channel", "title": "Bank claim"},
        "message_id": mid,
    }


def offer(mid=77, gid=1):
    return message(
        mid,
        gid=gid,
        text="🎁 Bonus: claim a payment",
        entities=[
            {
                "type": "text_link",
                "offset": 3,
                "length": 5,
                "url": "https://example.invalid/claim",
            }
        ],
    )


def test_quote_does_not_replace_full_reply_or_hide_link():
    content = extract(
        message(
            text="Thanks, paid!",
            reply_to_message=offer(),
            quote={"text": "Bonus", "position": 3},
        )
    )
    ctx = content["reply_context"]
    assert ctx["text"] == offer().text and ctx["quote"] == "Bonus"
    assert ctx["links"] == ["https://example.invalid/claim"]
    assert content["links"] == []
    assert content["quoted_context"] == offer().text
    assert not ctx["incomplete"]


@pytest.mark.parametrize("has_quote", [False, True])
def test_external_source_and_quote_coverage_are_explicit(has_quote):
    kwargs = {"quote": {"text": "Bonus payment", "position": 0}} if has_quote else {}
    ctx = extract(message(external_reply=external(), **kwargs))["reply_context"]
    assert ctx["source"]["chat_id"] == -9999
    assert ctx["coverage"] == ("quote_only" if has_quote else "missing")
    assert ctx["incomplete"] is (not has_quote)


@pytest.mark.parametrize(
    "origin",
    [
        {
            "type": "hidden_user",
            "date": 1794900000,
            "sender_user_name": "Private sender",
        },
        external()["origin"],
    ],
)
def test_forward_is_own_publication_without_reassigning_author(origin):
    msg = offer().model_copy(update={})
    msg = message(text=msg.text, entities=msg.entities, forward_origin=origin)
    content = extract(msg)
    assert content["own"] == offer().text and content["forwarded"]
    assert content["reply_context"] is None
    assert content["links"] == ["https://example.invalid/claim"]


@pytest.mark.parametrize("kind", ["long_text", "photo", "long_link"])
def test_truncated_or_unread_source_is_not_complete(kind):
    original = offer()
    if kind == "long_text":
        original = message(text="x" * 4097)
    elif kind == "photo":
        original = message(
            text=None,
            photo=[{"file_id": "x", "file_unique_id": "x", "width": 20, "height": 20}],
        )
    else:
        original = message(
            text="link",
            entities=[
                {
                    "type": "text_link",
                    "offset": 0,
                    "length": 4,
                    "url": "https://example.invalid/" + "x" * 1100,
                }
            ],
        )
    ctx = extract(message(reply_to_message=original))["reply_context"]
    assert ctx["incomplete"]


@pytest.mark.parametrize("form", ["reply", "quote", "forward"])
async def test_context_sent_to_model_and_cards_identical(db, bot, auth, scope, form):
    classifier = Classifier("SCAM")
    pipeline = Pipeline(db, bot, classifier, OCR())
    kwargs = {"reply_to_message": offer()}
    if form == "quote":
        kwargs = {
            "external_reply": external(),
            "quote": {"text": "Bonus payment", "position": 0},
        }
    elif form == "forward":
        kwargs = {"forward_origin": external()["origin"]}
    msg = message(text=offer().text if form == "forward" else "Thanks, paid!", **kwargs)
    await pipeline.ingest(msg, 1)
    await check_all(db, pipeline)
    sent_payload = classifier.calls[0][2]
    ctx = sent_payload["publication_context"][0]
    assert ctx["publication_index"] == 0
    if form == "reply":
        assert ctx["reply"]["text"] == offer().text
        assert ctx["reply"]["links"] == ["https://example.invalid/claim"]
        assert sent_payload["links"] == []
    assert ctx["forwarded"] is (form == "forward")
    async with db.connect() as conn:
        iid = (await conn.execute(select(t.incidents.c.id))).scalar_one()
        oid = (
            await conn.execute(
                select(t.outbox.c.id).where(t.outbox.c.kind == "incident")
            )
        ).scalar_one()
        actions = (await conn.execute(select(t.actions))).mappings().all()
        assert all(a["chat_id"] == 1 for a in actions)
        assert (
            await conn.execute(select(t.users.c.telegram_user_id))
        ).scalar_one() == 900
    await Delivery(db, bot, Onboarding(db, bot, auth)).execute(
        {"payload": {"id": str(oid)}}
    )
    await ui_for(db, bot, auth).show_card(scope, iid)
    texts = [c[2] for c in bot.calls if c[0] == "send_message"]
    marker = "📨 Пересланное сообщение" if form == "forward" else "💬 Комментарий:"
    assert len(texts) == 2 and all(marker in text for text in texts)
    if form == "reply":
        assert all(
            "↪️ Ответ на:" in text and "example.invalid/claim" in text for text in texts
        )


async def test_missing_context_is_review_not_trust_credit_and_replay_safe(db, bot):
    classifier = Classifier("OK")
    pipeline = Pipeline(db, bot, classifier, OCR())
    msg = message(text="Thanks, paid!", external_reply=external())
    await pipeline.ingest(msg, 1)
    await check_all(db, pipeline)
    await pipeline.ingest(msg, 1)
    await check_all(db, pipeline)
    assert not classifier.calls
    for table in (t.credits, t.actions, t.strikes, t.trust):
        assert await counts(db, table) == 0
    assert await counts(db, t.outbox) == 1
    async with db.connect() as conn:
        inc = (await conn.execute(select(t.incidents))).mappings().one()
        assert not inc["complete"] and inc["reason"] == "reply_context_incomplete"


async def test_trusted_incomplete_reply_still_has_no_external_checks(db, bot):
    await grant(db)
    bot.get_chat_member = AsyncMock(side_effect=RuntimeError("Must not run"))
    classifier = Classifier()
    pipeline = Pipeline(db, bot, classifier, OCR())
    await pipeline.ingest(message(external_reply=external()), 1)
    await check_all(db, pipeline)
    assert not classifier.calls and await counts(db, t.outbox) == 0
    bot.get_chat_member.assert_not_awaited()


@pytest.mark.parametrize("stored", [False, True])
async def test_full_length_reply_preserves_evidence_at_end(db, bot, stored):
    text = "я" * 4056 + " END_OF_SOURCE: prohibited offer"
    classifier = Classifier()
    pipeline = Pipeline(db, bot, classifier, OCR())
    original = message(77, text=text)
    if stored:
        await pipeline.ingest(original, 1)
        await check_all(db, pipeline)
        classifier.calls.clear()
        reply = message(78, text="Thanks", external_reply=external(-1001, 77))
    else:
        reply = message(78, text="Thanks", reply_to_message=original)
    await pipeline.ingest(reply, 2)
    await check_all(db, pipeline)
    assert len(classifier.calls) == 1
    context = classifier.calls[0][2]["publication_context"][0]["reply"]
    assert context["text"] == text and not context["incomplete"]


@pytest.mark.parametrize("source_case", ["local", "other_group", "edited"])
async def test_recovery_uses_only_existing_unedited_local_source(db, bot, source_case):
    classifier = Classifier()
    pipeline = Pipeline(db, bot, classifier, OCR())
    source = offer(gid=2 if source_case == "other_group" else 1)
    await pipeline.ingest(source, 1)
    await check_all(db, pipeline)
    if source_case == "edited":
        await pipeline.ingest(message(77, text="Changed offer"), 2)
        await check_all(db, pipeline)
    classifier.calls.clear()
    chat = -1002 if source_case == "other_group" else -1001
    await pipeline.ingest(message(text="Thanks", external_reply=external(chat, 77)), 3)
    await check_all(db, pipeline)
    if source_case == "local":
        ctx = classifier.calls[0][2]["publication_context"][0]["reply"]
        assert ctx["coverage"] == "stored_local" and ctx["text"] == offer().text
        assert ctx["links"] == ["https://example.invalid/claim"]
    else:
        assert not classifier.calls


async def test_context_edits_recheck_without_duplicate_strikes(db, bot):
    classifier = Classifier("SCAM")
    pipeline = Pipeline(db, bot, classifier, OCR())
    await pipeline.ingest(message(text="Thanks", reply_to_message=offer()), 1)
    await check_all(db, pipeline)
    await pipeline.ingest(
        message(text="Thanks", reply_to_message=message(77, text="Another offer")), 2
    )
    await check_all(db, pipeline)
    assert len(classifier.calls) == 2
    assert await counts(db, t.strikes) == 1


async def test_album_context_is_attributed_to_its_publication(db, bot):
    classifier = Classifier()
    pipeline = Pipeline(db, bot, classifier, OCR())
    await pipeline.ingest(
        message(1, text="Thanks", media_group_id="album", reply_to_message=offer()), 1
    )
    await pipeline.ingest(message(2, text="Second part", media_group_id="album"), 2)
    await check_all(db, pipeline)
    payload = classifier.calls[0][2]
    assert len(payload["own_publication"]) == 2
    assert len(payload["publication_context"]) == 1
    assert payload["publication_context"][0]["publication_index"] == 0


async def test_partial_context_review_never_sanctions_or_builds_trust(db, bot):
    classifier = Classifier("REVIEW")
    pipeline = Pipeline(db, bot, classifier, OCR())
    await pipeline.ingest(
        message(
            text="Thanks, paid!",
            external_reply=external(),
            quote={"text": "Bank", "position": 0},
        ),
        1,
    )
    await check_all(db, pipeline)
    assert len(classifier.calls) == 1
    assert await counts(db, t.actions) == 0 and await counts(db, t.trust) == 0
    async with db.connect() as conn:
        inc = (await conn.execute(select(t.incidents))).mappings().one()
        assert inc["status"] == "review" and not inc["complete"]
        assert inc["model_label"] == "REVIEW"
        assert not (await conn.execute(select(t.credits.c.eligible))).scalar_one()
    assert incident_text(inc).startswith("⚠️ Недостаточно контекста")
    assert "Уверенность модели" not in incident_text(inc)
    assert await counts(db, t.outbox) == 1
