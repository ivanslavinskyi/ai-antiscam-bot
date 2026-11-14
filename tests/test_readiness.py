import pytest

from app.v2 import tables as t
from app.v2.db import check_ready, check_bot_identity
from app.v2.domain import now


async def test_restart_after_all_groups_disconnected(db):
    async with db.begin() as conn:
        await conn.execute(
            t.migrations.insert().values(
                id="test-import", state="verified", manifest={"bot_id": 999001}
            )
        )
        await conn.execute(t.bindings.update().values(closed_at=now()))
        await conn.execute(t.group_settings.update().values(lifecycle="disconnected"))
    await check_ready(db)
    await check_bot_identity(db, 999001)
    with pytest.raises(RuntimeError, match="identity"):
        await check_bot_identity(db, 999002)
    async with db.begin() as conn:
        await conn.execute(
            t.roles.update().where(t.roles.c.role == "owner").values(revoked_at=now())
        )
    with pytest.raises(RuntimeError, match="owner"):
        await check_ready(db)


async def test_start_still_requires_verified_import(db):
    with pytest.raises(RuntimeError, match="verified data import"):
        await check_ready(db)
    async with db.begin() as conn:
        await conn.execute(
            t.migrations.insert().values(
                id="unfinished", state="pending", manifest={"bot_id": 999001}
            )
        )
    with pytest.raises(RuntimeError, match="identity"):
        await check_bot_identity(db, 999001)
