from datetime import timedelta

import pytest
from sqlalchemy import select

from app.v2 import tables as t
from app.v2.domain import Conflict, Denied, now
from app.v2.operator import parse_import, Operator
from app.v2.onboarding import Onboarding
from app.v2.delivery import Delivery
from conftest import message


def test_csv_expiry_validation_and_text_id():
    future = (now() + timedelta(days=7)).isoformat()
    row = parse_import(
        f"telegram_user_id,reason,expires_at\n'900,Verified,{future}\n".encode()
    )[0]
    assert row["telegram_user_id"] == 900 and row["expires_at"]
    for date in ["2020-01-01T00:00:00Z", "2030-01-01T12:00:00", "invalid"]:
        with pytest.raises(Conflict):
            parse_import(f"telegram_user_id,expires_at\n900,{date}\n".encode())


async def test_csv_import_preserves_expiry_and_exports_roundtrip(db, auth, scope):
    date = (now() + timedelta(days=7)).isoformat()
    operator = Operator(db, auth)
    rows = parse_import(
        f"telegram_user_id,reason,expires_at\n777,Verified,{date}\n".encode()
    )
    token = await operator.issue(scope, "import_trust", {"rows": rows})
    await operator.confirm(222, -2001, token)
    exported = await operator.export(scope, "trust")
    actual = parse_import(exported)
    assert (
        actual[0]["telegram_user_id"] == 777
        and actual[0]["expires_at"] == rows[0]["expires_at"]
    )


async def test_lost_onboarding_response_can_be_completed_from_original_card(
    db, bot, auth
):
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
        outbox = (await conn.execute(select(t.outbox))).mappings().one()
    bot.fail_send = True
    await Delivery(db, bot, wizard).execute(job)
    delivered = message(gid=1003, uid=bot.id).model_copy(
        update={"chat": message().chat.model_copy(update={"id": -2003})}
    )
    with pytest.raises(Denied):
        await wizard.acknowledge(111, -2003, rid, "wrong", delivered)
    assert (
        await wizard.acknowledge(
            111, -2003, rid, outbox["payload"]["receipt"], delivered
        )
        == gid
    )
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
                select(t.outbox.c.state).where(t.outbox.c.id == outbox["id"])
            )
        ).scalar_one() == "sent"
    assert len([c for c in bot.calls if c[0] == "send_message"]) == 1
