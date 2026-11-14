import uuid
from datetime import datetime

import pytest
from sqlalchemy import select, func

from app.v2 import tables as t
from app.v2.card_view import incident_text, code_entities, card_datetime
from app.v2.delivery import Delivery
from app.v2.domain import Conflict, Denied, Role, GroupScope
from app.v2.onboarding import Onboarding
from app.v2.operator import Operator
from app.v2.pipeline import Pipeline
from app.v2.ui import UI, incident_buttons
from conftest import message
from test_pipeline import Classifier, OCR, check_all


def ui_for(db, bot, auth):
    return UI(db, bot, auth, Operator(db, auth), Onboarding(db, bot, auth))


@pytest.mark.parametrize(
    "stamp,expected",
    [
        ("2026-11-14T16:25:19.172874+00:00", "14 ноября 2026, 17:25"),
        ("2026-07-14T22:25:19+00:00", "15 июля 2026, 00:25"),
        ("2026-11-14T16:25:19", "14 ноября 2026, 17:25"),
    ],
)
def test_card_time_handles_local_day_summer_time_and_legacy(stamp, expected):
    assert card_datetime(datetime.fromisoformat(stamp), "Europe/Zurich") == expected


@pytest.mark.parametrize(
    "zone,expected",
    [
        ("Europe/Zurich", "14 ноября 2026, 17:25"),
        ("Asia/Tokyo", "15 ноября 2026, 01:25"),
    ],
)
async def test_delivered_and_reopened_cards_use_group_timezone(
    db, bot, auth, scope, zone, expected
):
    async with db.begin() as conn:
        policy = (
            await conn.execute(
                select(t.group_settings.c.policy).where(t.group_settings.c.chat_id == 1)
            )
        ).scalar_one()
        await conn.execute(
            t.group_settings.update()
            .where(t.group_settings.c.chat_id == 1)
            .values(policy={**policy, "timezone": zone})
        )
    pipeline = Pipeline(db, bot, Classifier("SCAM"), OCR())
    await pipeline.ingest(message(), 1)
    await check_all(db, pipeline)
    async with db.begin() as conn:
        await conn.execute(
            t.incidents.update().values(
                created_at=datetime.fromisoformat("2026-11-14T16:25:19.172874+00:00")
            )
        )
        iid = (await conn.execute(select(t.incidents.c.id))).scalar_one()
        oid = (
            await conn.execute(
                select(t.outbox.c.id).where(t.outbox.c.kind == "incident").limit(1)
            )
        ).scalar_one()
    await Delivery(db, bot, Onboarding(db, bot, auth)).execute(
        {"payload": {"id": str(oid)}}
    )
    await ui_for(db, bot, auth).show_card(scope, iid)
    sent = [call for call in bot.calls if call[0] == "send_message"]
    assert len(sent) == 2
    for call in sent:
        assert "🕒 " + expected in call[2]
        assert "172874" not in call[2] and "+00:00" not in call[2]


def test_card_copyable_telegram_id_survives_emoji_and_untrusted_text():
    row = dict(
        id=uuid.uuid4(),
        chat_id=7,
        user_id=123,
        telegram_user_id=903858757,
        first_name="👩‍💻 <b>Имя</b>",
        last_name="😀",
        username="person",
        version=1,
        status="checked",
        human_label=None,
        model_label="SCAM",
        confidence=0.8,
        reason="<script>not markup</script>",
        active_strike=False,
        legacy=False,
        texts=[dict(text="Text (123) 🧑‍💼", ocr_text="")],
    )
    text = incident_text(row, "Группа <А>")
    assert text.startswith("⚠️ Обнаружен вероятный скам")
    assert "Уверенность модели: 80%" in text
    (entity,) = code_entities(text, row["telegram_user_id"])
    raw = text.encode("utf-16-le")
    assert (
        raw[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")
        == "903858757"
    )
    assert "<b>Имя</b>" in text  # Plain text, no parse_mode injection.
    assert all(
        len(b.callback_data.encode()) <= 64
        for line in incident_buttons(2147483647, row).inline_keyboard
        for b in line
    )
    row.update(user_id=None, telegram_user_id=None, confidence=None, status="error")
    text = incident_text(row)
    assert not code_entities(text, None)
    assert "вероятный скам" not in text
    assert "80%" not in text


async def test_quick_whitelist_is_local_and_stale_profile_cannot_reapply(
    db, bot, auth, scope
):
    pipeline = Pipeline(db, bot, Classifier(), OCR())
    for gid in (1, 2):
        await pipeline.ingest(message(gid=gid), gid)
    ui = ui_for(db, bot, auth)
    profile = await ui.operator.profile(scope, 1)
    await ui.member_action(scope, 1, "trust_add", profile["version"])
    async with db.connect() as conn:
        grants = (await conn.execute(select(t.trust))).mappings().all()
        assert len(grants) == 1 and grants[0]["chat_id"] == 1
        assert grants[0]["expires_at"] is None
        assert not (await conn.execute(select(t.actions.c.id))).first()
    with pytest.raises(Conflict):
        await ui.member_action(scope, 1, "trust_add", profile["version"])
    with pytest.raises(Denied):
        await ui.member_action(
            GroupScope(1, 333, -2001, 1, Role.OBSERVER), 1, "trust_remove"
        )


async def test_settings_presets_version_and_financial_boundary(db, bot, auth, scope):
    ui = ui_for(db, bot, auth)
    row = await ui.policy_row(scope)
    await ui.apply_setting(scope, {"auto_whitelist_threshold": 3}, row["version"])
    async with db.connect() as conn:
        policies = (
            (
                await conn.execute(
                    select(t.group_settings).order_by(t.group_settings.c.chat_id)
                )
            )
            .mappings()
            .all()
        )
        assert policies[0]["policy"]["auto_whitelist_threshold"] == 3
        assert policies[1]["policy"]["auto_whitelist_threshold"] == 5
    with pytest.raises(Conflict):
        await ui.apply_setting(scope, {"auto_whitelist_threshold": 10}, row["version"])
    with pytest.raises(Denied):
        await ui.apply_setting(scope, {"daily_budget_usd": 10}, row["version"] + 1)
    await ui.settings(scope)
    panel = [call for call in bot.calls if call[0] == "send_message"][-1]
    texts = " ".join(
        b.text for r in panel[3]["reply_markup"].inline_keyboard for b in r
    )
    assert "Режим" in texts and "Автовайтлист" in texts
    assert not any(word in texts for word in ("USD", "бюджет", "расход"))


async def test_auto_trust_card_is_immediate_scoped_and_old_grant_cannot_revoke_new(
    db, bot, auth, scope
):
    async with db.begin() as conn:
        policy = (
            await conn.execute(
                select(t.group_settings.c.policy).where(t.group_settings.c.chat_id == 1)
            )
        ).scalar_one()
        policy.update(
            auto_whitelist_threshold=1, quiet_start="00:00", quiet_end="23:59"
        )
        await conn.execute(
            t.group_settings.update()
            .where(t.group_settings.c.chat_id == 1)
            .values(policy=policy)
        )
    pipeline = Pipeline(db, bot, Classifier(), OCR())
    await pipeline.ingest(message(text="Safe message"), 1)
    await check_all(db, pipeline)
    async with db.connect() as conn:
        grant = dict((await conn.execute(select(t.trust))).mappings().one())
        notice = dict(
            (
                await conn.execute(
                    select(t.outbox).where(t.outbox.c.kind == "trust_notice")
                )
            )
            .mappings()
            .one()
        )
    assert notice["payload"]["grant_id"] == str(grant["id"])
    delivery = Delivery(db, bot, Onboarding(db, bot, auth))
    job = {"payload": {"id": str(notice["id"])}}
    await delivery.execute(job)
    await delivery.execute(job)
    sends = [c for c in bot.calls if c[0] == "send_message"]
    assert len(sends) == 1 and sends[0][1] == -2001
    assert "Добавлен в вайтлист автоматически" in sends[0][2]
    assert "1 из 1" in sends[0][2]
    assert sends[0][3]["entities"][0].type == "code"
    ui = ui_for(db, bot, auth)
    await ui.member_action(scope, grant["user_id"], "trust_remove", grant=grant["id"])
    await ui.member_action(scope, grant["user_id"], "trust_add")
    with pytest.raises(Conflict):
        await ui.member_action(
            scope, grant["user_id"], "trust_remove", grant=grant["id"]
        )
    assert (await ui.operator.profile(scope, grant["user_id"]))["trust"]


async def test_delete_from_card_does_not_repeat_or_label(db, bot, auth, scope):
    pipeline = Pipeline(db, bot, Classifier(), OCR())
    await pipeline.ingest(message(), 1)
    async with db.connect() as conn:
        incident = dict((await conn.execute(select(t.incidents))).mappings().one())
    ui = ui_for(db, bot, auth)
    for _ in range(2):
        await ui.execute_quick(
            scope, "delete", {"id": str(incident["id"])}, incident["version"]
        )
    async with db.connect() as conn:
        assert (
            await conn.execute(
                select(func.count())
                .select_from(t.actions)
                .where(t.actions.c.kind == "delete")
            )
        ).scalar_one() == 1
        current = (await conn.execute(select(t.incidents))).mappings().one()
        assert current["human_label"] is None
        assert not current["active_strike"]


async def test_mode_switch_checks_rights_preserves_trust_and_rejects_stale_panel(
    db, bot, auth, scope
):
    pipeline = Pipeline(db, bot, Classifier(), OCR())
    await pipeline.ingest(message(), 1)
    ui = ui_for(db, bot, auth)
    await ui.member_action(scope, 1, "trust_add")
    initial = await ui.policy_row(scope)
    await ui.execute_quick(
        scope,
        "mode",
        {"mode": "observe", "expected_policy_version": initial["version"]},
    )
    with pytest.raises(Conflict):
        await ui.execute_quick(
            scope,
            "mode",
            {"mode": "auto", "expected_policy_version": initial["version"]},
        )
    fresh = await ui.policy_row(scope)
    bot.members[(-1001, bot.id)] = "member"
    with pytest.raises(Conflict):
        await ui.execute_quick(
            scope, "mode", {"mode": "auto", "expected_policy_version": fresh["version"]}
        )
    assert (await ui.operator.profile(scope, 1))["trust"]
    assert (await ui.policy_row(scope))["mode"] == "observe"
