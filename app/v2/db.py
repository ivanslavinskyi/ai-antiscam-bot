from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def engine_for(url: str) -> AsyncEngine:
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("V2 requires PostgreSQL.")
    return create_async_engine(
        parsed.set(drivername="postgresql+psycopg"),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=3,
        hide_parameters=True,
    )


@asynccontextmanager
async def transaction(engine: AsyncEngine):
    async with engine.begin() as connection:
        yield connection


async def check_ready(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        revision = (
            await conn.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar()
        if revision != "0007_group_requests":
            raise RuntimeError(
                "Schema is not ready. Run the explicit migration procedure."
            )
        verified = (
            await conn.execute(
                text("SELECT count(*) FROM migration_runs WHERE state='verified'")
            )
        ).scalar_one()
        if not verified:
            raise RuntimeError("A verified data import is required before starting V2.")
        owners = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM role_grants WHERE role='owner' AND revoked_at IS NULL"
                )
            )
        ).scalar_one()
        if not owners:
            raise RuntimeError("An active system owner is required before starting V2.")
        # All groups may be intentionally disconnected. The owner's personal
        # menu must remain available after a restart to reconnect them.


async def check_bot_identity(engine: AsyncEngine, bot_id: int) -> None:
    from sqlalchemy import select
    from app.v2 import tables as t

    async with engine.connect() as conn:
        manifests = (
            (
                await conn.execute(
                    select(t.migrations.c.manifest).where(
                        t.migrations.c.state == "verified"
                    )
                )
            )
            .scalars()
            .all()
        )
    if not any(manifest.get("bot_id") == bot_id for manifest in manifests):
        raise RuntimeError("Bot identity does not match the verified import manifest.")
