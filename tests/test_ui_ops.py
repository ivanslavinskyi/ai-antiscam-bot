import uuid
from datetime import datetime, timedelta, timezone

import pytest
from aiogram.types import CallbackQuery
from sqlalchemy import select, func

from app.v2 import tables as t
from app.v2 import controls
from app.v2.domain import Policy, quiet_delay, GroupScope, Role, Denied
from app.v2.onboarding import Onboarding
from app.v2.operator import Operator
from app.v2.ui import UI, incident_buttons, clip
from app.v2.pipeline import Pipeline
from app.v2.queue import persist_updates
from aiogram.types import Update
from ops.backup import retention_keep
from tools.evaluate_models import validate_dataset, compare
from conftest import message
from test_pipeline import Classifier, OCR, check_all, counts
from test_boundaries import prepared_action
from app.v2.actions import ActionExecutor


def test_quiet_hours_cross_midnight_and_dst():
    policy = Policy(quiet_start="22:00", quiet_end="08:00")
    moment = datetime(2026, 11, 13, 23, 0, tzinfo=timezone.utc)
    assert quiet_delay(policy, moment) == 8 * 3600
    assert quiet_delay(policy, moment.replace(hour=11)) == 0


def test_retention_preserves_recent_daily_and_weekly():
    moment = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    names = [
        "antiscam-"
        + (moment - timedelta(hours=i)).strftime("%Y%m%dT%H%M%SZ")
        + ".dump.age"
        for i in range(24 * 70)
    ]
    keep = retention_keep(names, moment, daily=14, weekly=8)
    assert set(names[:24]) <= keep
    assert names[-1] not in keep
    assert 30 < len(keep) < 80
    assert "../unrelated" not in retention_keep(["../unrelated"], moment)


def test_callback_length_and_unicode_limits():
    row = {
        "id": uuid.uuid4(),
        "user_id": 2**31 - 1,
        "legacy": False,
        "version": 2**31 - 1,
    }
    kb = incident_buttons(2**31 - 1, row)
    assert all(
        len(b.callback_data.encode()) <= 64 for line in kb.inline_keyboard for b in line
    )
    assert len(clip("😀" * 5000).encode("utf-16-le")) <= 7202


def test_evaluation_cannot_claim_human_quality_from_old_labels():
    with pytest.raises(ValueError):
        validate_dataset(
            [
                {
                    "id": 1,
                    "text": "text",
                    "label": "SCAM",
                    "split": "holdout",
                    "review_source": "legacy_model",
                }
            ]
        )
    rows = [
        {"id": 1, "label": "NOT_SCAM", "must_protect_author": True},
        {"id": 2, "label": "SCAM"},
    ]
    old = [
        {"id": 1, "label": "OK", "confidence": 0.99, "cost_usd": 0.001},
        {"id": 2, "label": "SCAM", "confidence": 0.99, "cost_usd": 0.001},
    ]
    new = [dict(old[0], label="SCAM"), old[1]]
    assert not compare(rows, old, new)["passed"]


async def test_telegram_update_id_can_reset_after_inactivity(db):
    await persist_updates(db, 999001, [Update(update_id=100000, message=message(1))])
    await persist_updates(db, 999001, [Update(update_id=50, message=message(2))])
    async with db.connect() as conn:
        assert (await conn.execute(select(t.cursors.c.next_offset))).scalar_one() == 51


async def test_system_pause_blocks_automatic_not_manual_action(db, bot, auth):
    token = await controls.prepare(db, 111, True)
    await controls.confirm(db, 111, token)
    job = await prepared_action(db, "delete")
    await ActionExecutor(db, bot, auth).execute(job)
    assert not [c for c in bot.calls if c[0] == "delete_message"]
    with pytest.raises(Denied):
        await controls.prepare(db, 222, False)


async def test_archive_read_only_scope_can_read_never_moderate(db, bot, auth):
    p = Pipeline(db, bot, Classifier(), OCR())
    await p.ingest(message(), 1)
    await check_all(db, p)
    op = Operator(db, auth)
    archive = GroupScope(1, 111, 111, 0, Role.OWNER)
    page, _ = await op.page(archive, "history")
    assert len(page) == 1
    with pytest.raises(Denied):
        await op.issue(archive, "mode", {"mode": "auto"})


async def test_admin_ui_browse_and_confirmation_use_local_scope(db, bot, auth, scope):
    p = Pipeline(db, bot, Classifier("SCAM"), OCR())
    await p.ingest(message(), 1)
    await check_all(db, p)
    op = Operator(db, auth)
    ui = UI(db, bot, auth, op, Onboarding(db, bot, auth))
    await ui.menu(scope)
    await ui.page(scope, "history")
    await ui.profile(scope, 1)
    await ui.settings(scope)
    async with db.connect() as conn:
        iid = (await conn.execute(select(t.incidents.c.id))).scalar_one()
    query = CallbackQuery(
        id="query",
        from_user={"id": 222, "is_bot": False, "first_name": "Admin"},
        chat_instance="test",
        message=message().model_copy(
            update={"chat": message().chat.model_copy(update={"id": -2001})}
        ),
        data=f"v:label:1:{iid.hex}:NOT_SCAM",
    )
    await ui.callback(query)
    async with db.connect() as conn:
        assert (
            await conn.execute(select(func.count()).select_from(t.operations))
        ).scalar_one() == 0  # An old unversioned button only opens a fresh card.
        assert (
            await conn.execute(select(t.incidents.c.human_label))
        ).scalar_one() is None
    assert all(call[1] == -2001 for call in bot.calls if call[0] == "send_message")


async def test_legacy_edit_with_null_update_id_is_supported(db, bot):
    p = Pipeline(db, bot, Classifier(), OCR())
    await p.ingest(message(), 1)
    async with db.begin() as conn:
        await conn.execute(t.revisions.update().values(update_id=None))
        await conn.execute(t.incidents.update().values(legacy=True))
    await p.ingest(message(text="Edited legacy"), 2)
    await check_all(db, p)
    assert await counts(db, t.revisions) == 2
    assert await counts(db, t.actions) == 0
