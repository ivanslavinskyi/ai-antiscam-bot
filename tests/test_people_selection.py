import asyncio

import pytest
from aiogram.types import CallbackQuery, UsersShared, SharedUser
from sqlalchemy import select, func

from app.v2 import tables as t
from app.v2.domain import Conflict, Denied, now
from app.v2.onboarding import Onboarding
from app.v2.operator import Operator
from app.v2.ui import UI
from conftest import message


def private(text="", uid=111, **extra):
    msg = message(uid=uid, text=text, **extra)
    return msg.model_copy(
        update={"chat": msg.chat.model_copy(update={"id": uid, "type": "private"})}
    )


async def click(ui, data, uid=111):
    msg = private(uid=uid)
    await ui.callback(
        CallbackQuery(
            id="people",
            from_user=msg.from_user,
            chat_instance="test",
            message=msg,
            data=data,
        )
    )


def make_ui(db, bot, auth):
    return UI(db, bot, auth, Operator(db, auth), Onboarding(db, bot, auth))


async def test_manual_people_single_and_three_ids_require_final_continue(db, bot, auth):
    ui = make_ui(db, bot, auth)
    await click(ui, "v:newpeople")
    nonce = (await ui.session(111, 111))["state"]["draft"]
    await click(ui, f"v:peoplepick:{nonce}:manager")
    await ui.message(private(" 222 "))
    assert (await ui.session(111, 111))["state"]["people"]["manager"]["id"] == 222
    await ui.message(private("222 333 444"))
    async with db.connect() as conn:
        assert not (await conn.execute(select(t.onboarding.c.id))).all()
    await click(ui, f"v:peopledone:{nonce}")
    async with db.connect() as conn:
        row = (await conn.execute(select(t.onboarding))).mappings().one()
    assert (row["manager_id"], row["work_actor_id"], row["admin_actor_id"]) == (
        222,
        333,
        444,
    )
    assert not row["work_confirmed"] and not row["admin_confirmed"]
    markup = [c for c in bot.calls if c[0] == "send_message"][-1][3]["reply_markup"]
    buttons = [b.callback_data for r in markup.inline_keyboard for b in r]
    assert f"v:invite:{row['id'].hex}:work" in buttons
    assert f"v:invite:{row['id'].hex}:admin" in buttons
    await click(ui, f"v:peopledone:{nonce}")
    async with db.connect() as conn:
        assert (
            await conn.execute(select(func.count()).select_from(t.onboarding))
        ).scalar_one() == 1


async def test_native_person_selection_and_stale_requests(db, bot, auth):
    ui = make_ui(db, bot, auth)
    await ui.people_start(111)
    nonce = (await ui.session(111, 111))["state"]["draft"]
    await click(ui, f"v:peoplepick:{nonce}:work")
    request_id = (await ui.session(111, 111))["state"]["request_id"]
    shared = UsersShared(
        request_id=request_id, users=[SharedUser(user_id=333, first_name="Ольга")]
    )
    await ui.message(private(users_shared=shared))
    state = (await ui.session(111, 111))["state"]
    assert state["people"]["work"] == {"id": 333, "name": "Ольга"}
    assert state["people"]["admin"]["id"] == 111
    with pytest.raises(Conflict):
        await ui.people_message(private(users_shared=shared))
    await click(ui, f"v:peoplepick:{nonce}:admin")
    with pytest.raises(Conflict):
        await ui.people_message(private(users_shared=shared))
    await ui.people_start(111)
    with pytest.raises(Conflict):
        await ui.people_callback(111, 111, ["v", "peoplepick", nonce, "manager"])


async def test_people_invalid_ids_scope_and_revocation(db, bot, auth):
    ui = make_ui(db, bot, auth)
    with pytest.raises(Denied):
        await ui.people_start(222)
    await ui.people_start(111)
    nonce = (await ui.session(111, 111))["state"]["draft"]
    await click(ui, f"v:peoplepick:{nonce}:manager")
    for value in ("-10012", "0", "4.5", "x", str(2**52), str(bot.id)):
        with pytest.raises(Conflict):
            await ui.people_message(private(value))
    with pytest.raises(Denied):
        await ui.people_callback(111, -2001, ["v", "peopledone", nonce])
    async with db.begin() as conn:
        await conn.execute(
            t.roles.update().where(t.roles.c.actor_id == 111).values(revoked_at=now())
        )
    with pytest.raises(Denied):
        await ui.people_message(private("222"))


async def test_people_draft_atomic_creation_and_changed_selection(db, bot, auth):
    ui = make_ui(db, bot, auth)
    await ui.people_start(111)
    nonce = (await ui.session(111, 111))["state"]["draft"]
    with pytest.raises(Conflict):
        await ui.onboarding.create(111, 222, 111, 111, people_draft=nonce)
    ids = await asyncio.gather(
        *[
            ui.onboarding.create(111, 111, 111, 111, people_draft=nonce)
            for _ in range(2)
        ]
    )
    assert ids[0] == ids[1]
    async with db.connect() as conn:
        assert (
            await conn.execute(select(func.count()).select_from(t.onboarding))
        ).scalar_one() == 1


async def test_manual_group_id_checks_rights_and_keeps_existing_bindings(db, bot, auth):
    ui = make_ui(db, bot, auth)
    rid = await ui.onboarding.create(111)
    await click(ui, f"v:fallback:{rid.hex}:work")
    bot.members[(-3001, bot.id)] = "member"
    await ui.message(private("-3001"))
    row = await ui.onboarding.get(111, rid)
    assert not row["work_confirmed"]
    bot.members[(-3001, bot.id)] = "administrator"
    await ui.message(private("-3001"))
    row = await ui.onboarding.get(111, rid)
    assert row["work_confirmed"] and row["work_chat_id"] == -3001
    assert row["state"] == "pending"
    async with db.connect() as conn:
        assert (
            await conn.execute(select(func.count()).select_from(t.bindings))
        ).scalar_one() == 2
    assert all(c[1] == 111 for c in bot.calls if c[0] == "send_message")
