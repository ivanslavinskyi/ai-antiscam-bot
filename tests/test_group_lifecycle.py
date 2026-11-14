from sqlalchemy import select, func

from app.v2 import tables as t
from app.v2.config import Settings
from app.v2.domain import now
from app.v2.runtime import Runtime
from app.v2.operator import Operator
from app.v2.onboarding import Onboarding
from app.v2.ui import UI
from app.v2.pipeline import Pipeline
from conftest import message
from test_pipeline import Classifier, OCR, check_all


async def test_shared_admin_sessions_do_not_switch_other_actors(db, bot, auth):
    async with db.begin() as conn:
        await conn.execute(
            t.bindings.update()
            .where(t.bindings.c.chat_id == 2)
            .values(admin_chat_id=-2001)
        )
        await conn.execute(
            t.roles.insert().values(
                chat_id=2, actor_id=222, role="moderator", granted_by=111
            )
        )
    ui = UI(db, bot, auth, Operator(db, auth), Onboarding(db, bot, auth))
    await ui.menu(await auth.scope(222, -2001, 1))
    await ui.menu(await auth.scope(333, -2001, 2))
    await ui.menu(await auth.scope(222, -2001, 2))
    assert (await ui.session(333, -2001))["chat_id"] == 2
    await ui.menu(await auth.scope(222, -2001, 1))
    assert (await ui.session(333, -2001))["chat_id"] == 2
    assert (await ui.session(222, -2001))["chat_id"] == 1


async def test_telegram_group_id_migration_preserves_scope(db, bot, test_url):
    runtime = Runtime(db, bot, Settings(_env_file=None, db_url=test_url))
    await runtime.classifier.close()
    await runtime.chat_migration(message(migrate_to_chat_id=-100900))
    async with db.connect() as conn:
        assert set(
            (
                await conn.execute(
                    select(t.aliases.c.telegram_chat_id).where(t.aliases.c.chat_id == 1)
                )
            ).scalars()
        ) == {-1001, -100900}
        assert (
            await conn.execute(
                select(t.chats.c.telegram_chat_id).where(t.chats.c.id == 1)
            )
        ).scalar_one() == -100900
    await runtime.chat_migration(
        message().model_copy(
            update={
                "chat": message().chat.model_copy(update={"id": -2001}),
                "migrate_to_chat_id": -200900,
            }
        )
    )
    async with db.connect() as conn:
        binding = (
            (
                await conn.execute(
                    select(t.bindings).where(
                        t.bindings.c.chat_id == 1, t.bindings.c.closed_at.is_(None)
                    )
                )
            )
            .mappings()
            .one()
        )
        assert binding["admin_chat_id"] == -200900 and binding["version"] == 2


async def test_reconnect_archived_group_preserves_history_without_trust_inheritance(
    db, bot, auth
):
    pipeline = Pipeline(db, bot, Classifier(), OCR())
    await pipeline.ingest(message(gid=2), 1)
    await check_all(db, pipeline)
    async with db.begin() as conn:
        uid = (await conn.execute(select(t.users.c.id))).scalar_one()
        await conn.execute(
            t.trust.insert().values(
                chat_id=1, user_id=uid, source="manual", reason="Only first group"
            )
        )
        await conn.execute(
            t.bindings.update().where(t.bindings.c.chat_id == 2).values(closed_at=now())
        )
        await conn.execute(
            t.group_settings.update()
            .where(t.group_settings.c.chat_id == 2)
            .values(lifecycle="archived", mode="paused")
        )
    wizard = Onboarding(db, bot, auth)
    rid = await wizard.create(111)
    selection, _ = await wizard.selection(111, rid, "work")
    await wizard.share(111, selection, -1002)
    selection, _ = await wizard.selection(111, rid, "admin")
    await wizard.share(111, selection, -2003)
    row = await wizard.get(111, rid)
    gid = await wizard.confirm(111, rid, row["version"])
    assert gid == 2
    async with db.connect() as conn:
        assert (
            await conn.execute(select(func.count()).select_from(t.chats))
        ).scalar_one() == 2
        assert (
            await conn.execute(
                select(func.count())
                .select_from(t.incidents)
                .where(t.incidents.c.chat_id == 2)
            )
        ).scalar_one() == 1
        assert (
            await conn.execute(
                select(func.count()).select_from(t.trust).where(t.trust.c.chat_id == 2)
            )
        ).scalar_one() == 0
