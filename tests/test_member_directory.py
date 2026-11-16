import pytest
from datetime import datetime, timezone
from types import SimpleNamespace
from aiogram.types import CallbackQuery
from sqlalchemy import select, func

from app.v2 import tables as t
from app.v2.domain import Denied, GroupScope, Role, Policy
from app.v2.onboarding import Onboarding
from app.v2.operator import Operator
from app.v2.state import ensure_member, ensure_user
from app.v2.ui import UI
from conftest import message


@pytest.mark.parametrize(
    "can_write,until,expected,action",
    [
        (True, 0, "писать разрешено", "mute"),
        (False, 0, "писать запрещено · бессрочно", "unmute"),
        (False, 1900000000, "писать запрещено до", "unmute"),
    ],
)
async def test_profile_restricted_permissions_after_unmute(
    db, bot, auth, scope, can_write, until, expected, action
):
    uid = await seed(db, 1, 901)
    original = bot.get_chat_member

    async def get_member(chat, user):
        if chat == -1001 and user == 901:
            return SimpleNamespace(
                status="restricted",
                is_member=True,
                can_send_messages=can_write,
                until_date=datetime.fromtimestamp(until, timezone.utc),
            )
        return await original(chat, user)

    bot.get_chat_member = get_member
    ui = UI(db, bot, auth, Operator(db, auth), Onboarding(db, bot, auth))
    await ui.profile(scope, uid)
    sent = [call for call in bot.calls if call[0] == "send_message"][-1]
    assert expected in sent[2] and "1970" not in sent[2]
    callbacks = [
        button.callback_data
        for row in sent[3]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert any(value.endswith(":" + action) for value in callbacks)
    opposite = "unmute" if action == "mute" else "mute"
    assert not any(value.endswith(":" + opposite) for value in callbacks)


async def seed(db, gid, telegram_id, name="Анна", username=None):
    async with db.begin() as conn:
        uid = await ensure_user(conn, telegram_id, first_name=name, username=username)
        await ensure_member(conn, gid, uid)
    return uid


async def test_directory_scopes_names_and_escapes_wildcards(db, bot, auth, scope):
    a = await seed(db, 1, 901, "Анна", "anna_one")
    b = await seed(db, 1, 902, "Анна", "anna_two")
    await seed(db, 2, 903, "Анна", "other_group")
    op = Operator(db, auth)
    rows, more = await op.members(scope, "анн")
    assert {r["id"] for r in rows} == {a, b}
    assert not more
    assert (await op.members(scope, "%"))[0] == []
    assert (await op.members(scope, "@ANNA_ONE"))[0][0]["id"] == a
    with pytest.raises(Denied):
        await op.members(GroupScope(2, 222, -2001, 1, Role.MANAGER))


async def test_directory_pagination_has_no_overlap(db, bot, auth, scope):
    ids = {await seed(db, 1, 1000 + i) for i in range(14)}
    op = Operator(db, auth)
    first, more = await op.members(scope)
    second, last = await op.members(scope, offset=12)
    assert more and not last
    assert len(first) == 12 and len(second) == 2
    assert {r["id"] for r in first + second} == ids


async def test_name_search_requires_selection_and_profile_callback_is_scoped(
    db, bot, auth, scope
):
    a = await seed(db, 1, 901)
    other = await seed(db, 2, 902)
    ui = UI(db, bot, auth, Operator(db, auth), Onboarding(db, bot, auth))
    await ui.search_menu(scope)
    msg = message(uid=222, text="Анна")
    msg = msg.model_copy(update={"chat": msg.chat.model_copy(update={"id": -2001})})
    await ui.message(msg)
    sent = [c for c in bot.calls if c[0] == "send_message"][-1]
    callbacks = [
        b.callback_data for row in sent[3]["reply_markup"].inline_keyboard for b in row
    ]
    assert f"v:user:1:{a}" in callbacks
    assert f"v:user:1:{other}" not in callbacks
    assert "Результаты поиска" in sent[2]
    query = CallbackQuery(
        id="test",
        from_user=msg.from_user,
        chat_instance="test",
        message=msg,
        data=f"v:user:1:{other}",
    )
    await ui.callback(query)
    assert (
        "не найден в этой группе"
        in [c for c in bot.calls if c[0] == "send_message"][-1][2]
    )


async def test_hidden_forward_does_not_guess_identity_or_create_members(
    db, bot, auth, scope
):
    await seed(db, 1, 901, "Анна")
    ui = UI(db, bot, auth, Operator(db, auth), Onboarding(db, bot, auth))
    await ui.menu(scope)
    msg = message(
        uid=222,
        text="Forwarded",
        forward_origin={
            "type": "hidden_user",
            "date": 1700000000,
            "sender_user_name": "Анна",
        },
    )
    msg = msg.model_copy(update={"chat": msg.chat.model_copy(update={"id": -2001})})
    await ui.message(msg)
    sent = [c for c in bot.calls if c[0] == "send_message"][-1]
    assert "Telegram не передал профиль" in sent[2]
    async with db.connect() as conn:
        assert (
            await conn.execute(select(func.count()).select_from(t.memberships))
        ).scalar_one() == 1
        assert (
            await conn.execute(select(func.count()).select_from(t.operations))
        ).scalar_one() == 0


async def test_directory_rechecks_revoked_role(db, bot, auth, scope):
    from app.v2.domain import now

    async with db.begin() as conn:
        await conn.execute(
            t.roles.update().where(t.roles.c.actor_id == 222).values(revoked_at=now())
        )
    with pytest.raises(Denied):
        await Operator(db, auth).members(scope)


@pytest.mark.parametrize(
    "action,allowed,forbidden",
    [
        ("trust_add", "trusted", "scam"),
        ("deny_add", "scam", "trusted"),
        ("unban", "resolved", "scam"),
    ],
)
async def test_reason_buttons_match_action_and_reject_old_wrong_button(
    db, bot, auth, scope, action, allowed, forbidden
):
    uid = await seed(db, 1, 901)
    ui = UI(db, bot, auth, Operator(db, auth), Onboarding(db, bot, auth))
    msg = message(uid=222).model_copy(
        update={"chat": message().chat.model_copy(update={"id": -2001})}
    )
    query = CallbackQuery(
        id="reason",
        from_user=msg.from_user,
        chat_instance="test",
        message=msg,
        data=f"v:member:1:{uid}:{action}",
    )
    await ui.callback(query)
    sent = [c for c in bot.calls if c[0] == "send_message"][-1]
    callbacks = [
        b.callback_data for row in sent[3]["reply_markup"].inline_keyboard for b in row
    ]
    if action == "trust_add":
        assert "v:duration:1:0" in callbacks
        assert not any(c.startswith("v:reason:") for c in callbacks)
    else:
        assert f"v:reason:1:{allowed}" in callbacks
    assert f"v:reason:1:{forbidden}" not in callbacks
    await ui.callback(query.model_copy(update={"data": f"v:reason:1:{forbidden}"}))
    async with db.connect() as conn:
        assert (
            await conn.execute(select(func.count()).select_from(t.operations))
        ).scalar_one() == 0
    assert "не подходит" in [c for c in bot.calls if c[0] == "send_message"][-1][2]


async def test_trust_confirmation_has_specific_receipt_and_cancel_clears_form(
    db, bot, auth, scope
):
    uid = await seed(db, 1, 901)
    await seed(db, 2, 901)
    op = Operator(db, auth)
    ui = UI(db, bot, auth, op, Onboarding(db, bot, auth))
    msg = message(uid=222).model_copy(
        update={"chat": message().chat.model_copy(update={"id": -2001})}
    )
    query = CallbackQuery(
        id="trust",
        from_user=msg.from_user,
        chat_instance="test",
        message=msg,
        data=f"v:member:1:{uid}:trust_add",
    )
    await ui.callback(query)
    await ui.callback(query.model_copy(update={"data": f"v:user:1:{uid}"}))
    assert not (await ui.session(222, -2001))["state"].get("form")
    await ui.callback(query)
    await ui.callback(query.model_copy(update={"data": "v:reason:1:mistake"}))
    assert "не подходит" in [c for c in bot.calls if c[0] == "send_message"][-1][2]
    await ui.callback(query.model_copy(update={"data": "v:duration:1:7"}))
    async with db.connect() as conn:
        token = (await conn.execute(select(t.operations.c.token))).scalar_one()
    result = await op.confirm(222, -2001, token)
    assert "в вайтлисте этой рабочей группы" in result["message"]
    async with db.connect() as conn:
        assert (await conn.execute(select(t.trust.c.chat_id))).scalar_one() == 1
        grant = (await conn.execute(select(t.trust))).mappings().one()
        assert grant["reason"] == "Проверенный участник"
        assert (
            6.99
            < (grant["expires_at"] - grant["created_at"]).total_seconds() / 86400
            < 7.01
        )
        assert (
            await conn.execute(select(func.count()).select_from(t.actions))
        ).scalar_one() == 0


async def test_onboarding_names_and_completed_view_have_no_connect_button(
    db, bot, auth
):
    onboarding = Onboarding(db, bot, auth)
    rid = await onboarding.create(111)
    async with db.begin() as conn:
        await conn.execute(
            t.onboarding.update()
            .where(t.onboarding.c.id == rid)
            .values(
                work_chat_id=-3001,
                admin_chat_id=-3002,
                work_confirmed=True,
                admin_confirmed=True,
                state="connected",
            )
        )
    ui = UI(db, bot, auth, Operator(db, auth), onboarding)
    await ui.show_onboarding(111, rid)
    sent = [c for c in bot.calls if c[0] == "send_message"][-1]
    assert "Selected group (-3001)" in sent[2]
    assert "Selected group (-3002)" in sent[2]
    assert "Подключена" in sent[2]
    assert "вайтлист" not in sent[2].lower()
    assert not any(
        b.callback_data.startswith("v:connect:")
        for row in sent[3]["reply_markup"].inline_keyboard
        for b in row
    )


@pytest.mark.parametrize(
    "initial_policy",
    [{}, {"auto_whitelist_enabled": False, "auto_whitelist_threshold": 17}],
)
async def test_onboarding_hides_trust_and_old_buttons_preserve_policy(
    db, bot, auth, initial_policy
):
    onboarding = Onboarding(db, bot, auth)
    rid = await onboarding.create(111)
    async with db.begin() as conn:
        await conn.execute(
            t.onboarding.update()
            .where(t.onboarding.c.id == rid)
            .values(initial_policy=initial_policy)
        )
    ui = UI(db, bot, auth, Operator(db, auth), onboarding)
    before = await onboarding.get(111, rid)
    version = str(before["version"])
    await ui.show_onboarding(111, rid)
    await ui.design_private_callback(111, 111, ["v", "apsettings", rid.hex, version])
    for sent in [c for c in bot.calls if c[0] == "send_message"]:
        assert "вайтлист" not in sent[2].lower()
        assert "довер" not in sent[2].lower()
        buttons = [b for row in sent[3]["reply_markup"].inline_keyboard for b in row]
        assert not any("вайтлист" in b.text.lower() for b in buttons)
        assert not any(
            b.callback_data.startswith("v:apvalue:")
            and not b.callback_data.endswith(":admins")
            for b in buttons
        )
    for old_value in ("toggle", "3", "5", "10"):
        await ui.design_private_callback(
            111, 111, ["v", "apvalue", rid.hex, version, old_value]
        )
    after = await onboarding.get(111, rid)
    assert after["initial_policy"] == before["initial_policy"]
    assert after["version"] == before["version"]
    with pytest.raises(Denied):
        await onboarding.configure(
            111, rid, int(version), {"auto_whitelist_threshold": 3}
        )
    await ui.design_private_callback(
        111, 111, ["v", "apvalue", rid.hex, version, "admins"]
    )
    after = await onboarding.get(111, rid)
    original_policy = Policy.model_validate(before["initial_policy"]).model_dump()
    assert after["initial_policy"]["exempt_admins"] != original_policy["exempt_admins"]
    for key in ("auto_whitelist_threshold", "auto_whitelist_enabled"):
        assert after["initial_policy"][key] == original_policy[key]
