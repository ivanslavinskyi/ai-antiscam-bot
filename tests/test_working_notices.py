import pytest
from sqlalchemy import select

from app.v2 import tables as t
from app.v2.actions import ActionExecutor, warning_text
from app.v2.operator import Operator
from app.v2.pipeline import Pipeline
from conftest import message
from test_pipeline import Classifier, OCR, check_all


def test_mention_without_username_is_utf16_safe_and_not_markup():
    author = dict(
        telegram_user_id=900,
        first_name="👩‍💻 <b>Имя</b>",
        last_name="😀",
        username=None,
    )
    text, (entity,) = warning_text(
        {"actor_id": 222, "payload": {"deletion_notice": True}}, author
    )
    assert entity.type == "text_mention" and entity.user.id == 900
    assert (
        text.encode("utf-16-le")[
            entity.offset * 2 : (entity.offset + entity.length) * 2
        ].decode("utf-16-le")
        == "👩‍💻 <b>Имя</b> 😀"
    )
    assert "удалено модератором" in text
    text, entities = warning_text(
        {"actor_id": None, "payload": {"deletion_notice": True}}, None
    )
    assert not entities and "удалено" in text


async def execute_pending(db, executor):
    for _ in range(4):
        async with db.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(t.actions.c.id).where(t.actions.c.state == "planned")
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            return
        for aid in rows:
            await executor.execute({"payload": {"id": str(aid)}})
    raise AssertionError("Actions did not finish")


async def test_manual_delete_mentions_author_once_without_strike(db, bot, auth, scope):
    p = Pipeline(db, bot, Classifier(), OCR())
    await p.ingest(message(), 1)
    async with db.connect() as conn:
        inc = (await conn.execute(select(t.incidents))).mappings().one()
    op = Operator(db, auth)
    token = await op.issue(scope, "delete", {"id": str(inc["id"])}, inc["version"])
    await op.confirm(222, -2001, token)
    executor = ActionExecutor(db, bot, auth)
    await execute_pending(db, executor)
    async with db.connect() as conn:
        deletion = (
            await conn.execute(
                select(t.actions.c.id).where(t.actions.c.kind == "delete")
            )
        ).scalar_one()
        assert not (await conn.execute(select(t.strikes))).first()
        assert (
            await conn.execute(select(t.incidents.c.human_label))
        ).scalar_one() is None
        cleanup = (
            (
                await conn.execute(
                    select(t.jobs).where(t.jobs.c.kind == "notice_cleanup")
                )
            )
            .mappings()
            .one()
        )
        assert cleanup["payload"]["chat_id"] == -1001
    # Repeated completion cannot enqueue a duplicate notification.
    await executor.set_state(deletion, "succeeded")
    await execute_pending(db, executor)
    sent = [c for c in bot.calls if c[0] == "send_message"]
    assert len(sent) == 1 and sent[0][1] == -1001
    assert sent[0][3]["entities"][0].user.id == 900
    assert sent[0][3]["parse_mode"] is None


async def test_auto_warning_and_following_deletion_have_one_mention_each(db, bot, auth):
    p = Pipeline(db, bot, Classifier("SCAM"), OCR())
    executor = ActionExecutor(db, bot, auth)
    for mid in (1, 2):
        await p.ingest(message(mid=mid), mid)
        await check_all(db, p)
        await execute_pending(db, executor)
    sent = [c for c in bot.calls if c[0] == "send_message"]
    assert len(sent) == 2
    assert all(c[1] == -1001 and c[3]["entities"][0].user.id == 900 for c in sent)
    async with db.connect() as conn:
        assert (await conn.execute(select(t.member_states.c.balance))).scalar_one() == 2


async def test_unknown_manual_deletion_does_not_claim_success(db, bot, auth, scope):
    p = Pipeline(db, bot, Classifier(), OCR())
    await p.ingest(message(), 1)
    async with db.connect() as conn:
        inc = (await conn.execute(select(t.incidents))).mappings().one()
    op = Operator(db, auth)
    token = await op.issue(scope, "delete", {"id": str(inc["id"])}, inc["version"])
    await op.confirm(222, -2001, token)
    bot.fail_action = True
    await execute_pending(db, ActionExecutor(db, bot, auth))
    assert not [c for c in bot.calls if c[0] == "send_message"]
    async with db.connect() as conn:
        assert (await conn.execute(select(t.actions.c.state))).scalar_one() == "unknown"


async def test_manual_album_has_one_notice_and_does_not_touch_other_group(
    db, bot, auth, scope
):
    pipeline = Pipeline(db, bot, Classifier(), OCR())
    for mid in (1, 2):
        await pipeline.ingest(message(mid, media_group_id="album"), mid)
    await pipeline.ingest(message(gid=2), 3)
    async with db.connect() as conn:
        incident = (
            (await conn.execute(select(t.incidents).where(t.incidents.c.chat_id == 1)))
            .mappings()
            .one()
        )
    operator = Operator(db, auth)
    token = await operator.issue(
        scope, "delete", {"id": str(incident["id"])}, incident["version"]
    )
    await operator.confirm(222, -2001, token)
    await execute_pending(db, ActionExecutor(db, bot, auth))
    deleted = [c for c in bot.calls if c[0] == "delete_message"]
    sent = [c for c in bot.calls if c[0] == "send_message"]
    assert {(c[1], c[2]) for c in deleted} == {(-1001, 1), (-1001, 2)}
    assert len(sent) == 1 and sent[0][1] == -1001
    assert sent[0][3]["entities"][0].user.id == 900
    async with db.connect() as conn:
        assert not (
            await conn.execute(select(t.actions).where(t.actions.c.chat_id == 2))
        ).first()
        assert not (await conn.execute(select(t.strikes))).first()


@pytest.mark.parametrize("change", ["binding", "policy", "role"])
async def test_pending_notice_rechecks_scope_after_deletion(
    db, bot, auth, scope, change
):
    pipeline = Pipeline(db, bot, Classifier(), OCR())
    await pipeline.ingest(message(), 1)
    async with db.connect() as conn:
        incident = (await conn.execute(select(t.incidents))).mappings().one()
    operator = Operator(db, auth)
    token = await operator.issue(
        scope, "delete", {"id": str(incident["id"])}, incident["version"]
    )
    await operator.confirm(222, -2001, token)
    executor = ActionExecutor(db, bot, auth)
    async with db.connect() as conn:
        deletion = (await conn.execute(select(t.actions.c.id))).scalar_one()
    await executor.execute({"payload": {"id": str(deletion)}})
    async with db.begin() as conn:
        if change == "binding":
            await conn.execute(
                t.bindings.update()
                .where(t.bindings.c.chat_id == 1)
                .values(version=2, admin_chat_id=-2003)
            )
        elif change == "policy":
            await conn.execute(
                t.group_settings.update()
                .where(t.group_settings.c.chat_id == 1)
                .values(version=2)
            )
        else:
            await conn.execute(
                t.roles.delete().where(
                    t.roles.c.chat_id == 1, t.roles.c.actor_id == 222
                )
            )
    await execute_pending(db, executor)
    assert not [c for c in bot.calls if c[0] == "send_message"]
    async with db.connect() as conn:
        notice = (
            (await conn.execute(select(t.actions).where(t.actions.c.kind == "warn")))
            .mappings()
            .one()
        )
        assert notice["state"] in {"cancelled", "failed"}


async def test_unknown_notice_delivery_is_not_repeated(db, bot, auth, scope):
    pipeline = Pipeline(db, bot, Classifier(), OCR())
    await pipeline.ingest(message(), 1)
    async with db.connect() as conn:
        incident = (await conn.execute(select(t.incidents))).mappings().one()
    operator = Operator(db, auth)
    token = await operator.issue(
        scope, "delete", {"id": str(incident["id"])}, incident["version"]
    )
    await operator.confirm(222, -2001, token)
    bot.fail_send = True
    executor = ActionExecutor(db, bot, auth)
    await execute_pending(db, executor)
    async with db.connect() as conn:
        notice = (
            (await conn.execute(select(t.actions).where(t.actions.c.kind == "warn")))
            .mappings()
            .one()
        )
    assert notice["state"] == "unknown"
    bot.fail_send = False
    await executor.execute({"payload": {"id": str(notice["id"])}})
    assert len([c for c in bot.calls if c[0] == "send_message"]) == 1
