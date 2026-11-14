import re

import pytest
from aiogram.types import User, CallbackQuery
from sqlalchemy import select

from app.v2 import tables as t
from app.v2.domain import Denied, Conflict, now
from app.v2.pipeline import Pipeline
from app.v2.delivery import Delivery
from app.v2.actions import ActionExecutor
from app.v2.team_ui import remember_contact
from conftest import message
from test_design_atlas import ui_for
from test_pipeline import Classifier, OCR, check_all


async def test_shared_feed_search_respects_local_roles_and_current_membership(
    db, bot, auth
):
    p = Pipeline(db, bot, Classifier("SCAM"), OCR())
    for gid in (1, 2):
        await p.ingest(message(gid=gid, text=f"unique-{gid}"), gid)
    await check_all(db, p)
    async with db.begin() as conn:
        await conn.execute(
            t.bindings.update()
            .where(t.bindings.c.chat_id == 2)
            .values(admin_chat_id=-2001)
        )
    ui = ui_for(db, bot, auth)
    rows, _ = await ui.feed_rows(111, -2001)
    assert {r["chat_id"] for r in rows} == {1, 2}
    rows, _ = await ui.feed_rows(222, -2001)
    assert {r["chat_id"] for r in rows} == {1}
    assert not (await ui.feed_rows(222, -2001, {"query": "unique-2"}))[0]
    assert len((await ui.feed_rows(111, -2001, {"query": "unique-2"}))[0]) == 1
    assert not (await ui.feed_rows(111, -2001, {"query": "%"}))[0]
    bot.members[(-2001, 222)] = "left"
    with pytest.raises(Denied):
        await ui.feed_rows(222, -2001)


async def test_team_invitation_registers_candidate_without_granting_privileges(
    db, bot, auth, scope
):
    ui = ui_for(db, bot, auth)
    await ui.team_invite(scope)
    invite_text = [c[2] for c in bot.calls if c[0] == "send_message"][-1]
    token = re.search(r"start=team_(\S+)", invite_text).group(1)
    user = User(id=777, is_bot=False, first_name="Кандидат", username="candidate")
    bot.members[(-2001, 777)] = "left"
    with pytest.raises(Denied):
        await ui.accept_team_invite(user, token)
    bot.members[(-2001, 777)] = "member"
    await ui.accept_team_invite(user, token)
    async with db.connect() as conn:
        assert not (
            await conn.execute(select(t.roles).where(t.roles.c.actor_id == 777))
        ).first()
        assert (
            await conn.execute(
                select(t.admin_contacts.c.name).where(
                    t.admin_contacts.c.actor_id == 777
                )
            )
        ).scalar_one() == "Кандидат"
    with pytest.raises(Conflict):
        await ui.accept_team_invite(
            User(id=778, is_bot=False, first_name="Другой"), token
        )
    await ui.team_user(scope, 777)
    send = [c for c in bot.calls if c[0] == "send_message"][-1]
    button = send[3]["reply_markup"].inline_keyboard[0][0]
    await ui.operator.confirm(222, -2001, button.callback_data.split(":")[-1])
    async with db.connect() as conn:
        role = (
            (await conn.execute(select(t.roles).where(t.roles.c.actor_id == 777)))
            .mappings()
            .one()
        )
        assert role["chat_id"] == 1 and role["role"] == "moderator"
    with pytest.raises(Denied):
        await auth.scope(777, -2002, 2)


async def test_transfer_manager_is_owner_only_versioned_and_keeps_data(
    db, bot, auth, scope
):
    ui = ui_for(db, bot, auth)
    async with db.begin() as conn:
        await remember_contact(
            conn, -2001, User(id=777, is_bot=False, first_name="Новый")
        )
    with pytest.raises(Denied):
        await ui.group_callback(222, 222, ["v", "managerchange", "1", "777", "ask"])
    await ui.group_callback(111, 111, ["v", "managerchange", "1", "777", "ask"])
    send = [c for c in bot.calls if c[0] == "send_message"][-1]
    buttons = send[3]["reply_markup"].inline_keyboard
    token = buttons[0][0].callback_data.split(":")[-1]
    stale = buttons[1][0].callback_data.split(":")[-1]
    await ui.group_callback(111, 111, ["v", "oc", token])
    async with db.connect() as conn:
        roles = (
            (
                await conn.execute(
                    select(t.roles).where(
                        t.roles.c.chat_id == 1, t.roles.c.revoked_at.is_(None)
                    )
                )
            )
            .mappings()
            .all()
        )
        assert [(r["actor_id"], r["role"]) for r in roles] == [(777, "manager")]
        assert (
            await conn.execute(
                select(t.bindings.c.admin_chat_id).where(t.bindings.c.chat_id == 1)
            )
        ).scalar_one() == -2001
        assert (
            await conn.execute(
                select(t.roles.c.actor_id).where(
                    t.roles.c.chat_id == 2, t.roles.c.revoked_at.is_(None)
                )
            )
        ).scalar_one() == 333
    with pytest.raises(Conflict):
        await ui.group_callback(111, 111, ["v", "oc", stale])


async def test_card_manual_ban_tracks_source_and_stale_source_cannot_apply(
    db, bot, auth, scope
):
    p = Pipeline(db, bot, Classifier("SCAM"), OCR())
    await p.ingest(message(), 1)
    await check_all(db, p)
    ui = ui_for(db, bot, auth)
    async with db.connect() as conn:
        inc = (await conn.execute(select(t.incidents))).mappings().one()
    profile = await ui.operator.profile(scope, inc["user_id"])
    payload = {
        "user_id": inc["user_id"],
        "source_incident": str(inc["id"]),
        "source_version": inc["version"],
    }
    token = await ui.operator.issue(scope, "ban", payload, profile["version"])
    await ui.operator.confirm(scope.actor_id, scope.admin_chat_id, token)
    async with db.connect() as conn:
        action = (
            (await conn.execute(select(t.actions).where(t.actions.c.kind == "ban")))
            .mappings()
            .one()
        )
        assert (
            action["incident_id"] == inc["id"]
            and action["object_version"] == inc["version"]
        )
    profile = await ui.operator.profile(scope, inc["user_id"])
    repeated = await ui.operator.issue(scope, "ban", payload, profile["version"])
    with pytest.raises(Conflict):
        await ui.operator.confirm(scope.actor_id, scope.admin_chat_id, repeated)
    profile = await ui.operator.profile(scope, inc["user_id"])
    token = await ui.operator.issue(scope, "trust_add", payload, profile["version"])
    async with db.begin() as conn:
        await conn.execute(
            t.incidents.update().values(version=t.incidents.c.version + 1)
        )
    with pytest.raises(Conflict):
        await ui.operator.confirm(scope.actor_id, scope.admin_chat_id, token)
    async with db.connect() as conn:
        assert not (await conn.execute(select(t.trust))).first()


async def test_lifecycle_request_is_durable_deduplicated_and_owner_private(
    db, bot, auth, scope
):
    ui = ui_for(db, bot, auth)
    for _ in range(2):
        await ui.lifecycle_callback(scope, ["v", "lreq", "1", "rebind"])
    async with db.connect() as conn:
        requests = (await conn.execute(select(t.group_requests))).mappings().all()
        assert len(requests) == 1 and requests[0]["state"] == "open"
        outbox = (await conn.execute(select(t.outbox))).mappings().one()
        assert outbox["destination"] == 111 and outbox["kind"] == "group_request"
        assert (
            await conn.execute(
                select(t.bindings.c.admin_chat_id).where(t.bindings.c.chat_id == 1)
            )
        ).scalar_one() == -2001
    with pytest.raises(Denied):
        await ui.group_callback(222, 222, ["v", "grouprequests", "0"])
    with pytest.raises(Denied):
        await ui.group_callback(111, -2001, ["v", "grouprequests", "0"])
    await ui.group_callback(111, 111, ["v", "grouprequests", "0"])
    async with db.begin() as conn:
        await conn.execute(
            t.roles.update().where(t.roles.c.actor_id == 111).values(revoked_at=now())
        )
    await Delivery(db, bot, ui.onboarding).execute(
        {"payload": {"id": str(outbox["id"])}}
    )
    async with db.connect() as conn:
        assert (
            await conn.execute(select(t.outbox.c.state))
        ).scalar_one() == "cancelled"


async def test_onboarding_manager_choice_requires_membership_owner_and_current_version(
    db, bot, auth
):
    ui = ui_for(db, bot, auth)
    rid = await ui.onboarding.create(111)
    selection, _ = await ui.onboarding.selection(111, rid, "admin")
    await ui.onboarding.share(111, selection, -5000)
    row = await ui.onboarding.get(111, rid)
    bot.members[(-5000, 777)] = "left"
    with pytest.raises(Denied):
        await ui.onboarding.set_manager(111, rid, row["version"], 777)
    bot.members[(-5000, 777)] = "member"
    await ui.onboarding.set_manager(111, rid, row["version"], 777)
    with pytest.raises(Conflict):
        await ui.onboarding.set_manager(111, rid, row["version"], 111)
    assert (await ui.onboarding.get(111, rid))["manager_id"] == 777


async def test_simple_setting_updates_existing_panel_and_cancels_text_form(
    db, bot, auth, scope
):
    ui = ui_for(db, bot, auth)
    settings = await ui.policy_row(scope)
    await ui.save_session(222, -2001, 1, form="setting", field="warn_at")
    query = CallbackQuery(
        id="toggle",
        from_user={"id": 222, "is_bot": False, "first_name": "Manager"},
        chat_instance="test",
        message=message().model_copy(
            update={"chat": message().chat.model_copy(update={"id": -2001})}
        ),
        data=f"v:sp:1:auto_whitelist_enabled:{settings['version']}",
    )
    await ui.callback(query)
    edits = [call for call in bot.calls if call[0] == "edit_message_text"]
    assert edits and edits[-1][2]["message_id"] == query.message.message_id
    assert (await ui.policy_row(scope))["policy"]["auto_whitelist_enabled"] is False
    assert not (await ui.session(222, -2001))["state"].get("form")


async def test_explicit_unban_preserves_acknowledged_blacklist_but_rejects_new_one(
    db, bot, auth, scope
):
    p = Pipeline(db, bot, Classifier(), OCR())
    await p.ingest(message(), 1)
    ui = ui_for(db, bot, auth)
    await ui.execute_quick(
        scope,
        "deny_add",
        {"user_id": 1},
        (await ui.operator.profile(scope, 1))["version"],
    )
    bot.members[(-1001, 900)] = "kicked"
    await ui.member_action(scope, 1, "unban")
    sent = [c for c in bot.calls if c[0] == "send_message"][-1]
    assert "остаётся в блэклисте" in sent[2]
    token = sent[3]["reply_markup"].inline_keyboard[0][0].callback_data.split(":")[-1]
    await ui.operator.confirm(222, -2001, token)
    async with db.connect() as conn:
        action = (
            (await conn.execute(select(t.actions).where(t.actions.c.kind == "unban")))
            .mappings()
            .one()
        )
        deny = (await conn.execute(select(t.deny))).mappings().one()
    executor = ActionExecutor(db, bot, auth)
    await executor.execute({"payload": {"id": str(action["id"])}})
    assert any(c[0] == "unban_chat_member" for c in bot.calls)
    async with db.connect() as conn:
        assert (await conn.execute(select(t.deny.c.revoked_at))).scalar_one() is None
        assert not await executor.blacklist_blocks_release(conn, action)
    await ui.execute_quick(
        scope,
        "deny_remove",
        {"user_id": 1},
        (await ui.operator.profile(scope, 1))["version"],
    )
    await ui.execute_quick(
        scope,
        "deny_add",
        {"user_id": 1},
        (await ui.operator.profile(scope, 1))["version"],
    )
    async with db.connect() as conn:
        assert await executor.blacklist_blocks_release(conn, action)
        assert (
            await conn.execute(
                select(t.deny.c.revoked_at).where(t.deny.c.id == deny["id"])
            )
        ).scalar_one() is not None
