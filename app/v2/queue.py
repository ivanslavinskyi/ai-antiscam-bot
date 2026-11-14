import asyncio
from datetime import timedelta
import uuid

from sqlalchemy import select, text, or_
from sqlalchemy.dialects.postgresql import insert
from app.v2 import tables as t
from app.v2.domain import now


async def enqueue(conn, kind, key, payload, chat_id=None, priority=10, due_at=None):
    stmt = insert(t.jobs).values(
        kind=kind,
        key=key,
        payload=payload,
        chat_id=chat_id,
        priority=priority,
        due_at=due_at or now(),
    )
    await conn.execute(stmt.on_conflict_do_nothing(index_elements=["key"]))


async def emit(
    conn, *, destination, kind, payload, key, chat_id=None, binding_version=None
):
    result = await conn.execute(
        insert(t.outbox)
        .values(
            destination=destination,
            kind=kind,
            payload=payload,
            key=key,
            chat_id=chat_id,
            binding_version=binding_version,
        )
        .on_conflict_do_nothing(index_elements=["key"])
        .returning(t.outbox.c.id)
    )
    oid = result.scalar_one_or_none()
    if oid:
        await enqueue(
            conn, "outbox", "outbox:" + str(oid), {"id": str(oid)}, chat_id, priority=1
        )
    return oid


async def persist_updates(engine, bot_id, updates):
    async with engine.begin() as conn:
        accepted_new = False
        for incoming in updates:
            payload = incoming.model_dump(mode="json", exclude_none=True)
            publication = incoming.message or incoming.edited_message
            gid = None
            admin_context = False
            if publication and publication.chat.type != "private":
                gid, admin_context = (
                    await conn.execute(
                        select(
                            select(t.aliases.c.chat_id)
                            .where(t.aliases.c.telegram_chat_id == publication.chat.id)
                            .scalar_subquery(),
                            select(t.bindings.c.id)
                            .where(
                                t.bindings.c.admin_chat_id == publication.chat.id,
                                t.bindings.c.closed_at.is_(None),
                            )
                            .exists(),
                        )
                    )
                ).one()
            result = await conn.execute(
                insert(t.inbox)
                .values(
                    bot_id=bot_id,
                    update_id=incoming.update_id,
                    payload=payload,
                    chat_id=gid,
                )
                .on_conflict_do_nothing(index_elements=["bot_id", "update_id"])
                .returning(t.inbox.c.id)
            )
            iid = result.scalar_one_or_none()
            if iid:
                accepted_new = True
                high = bool(
                    incoming.callback_query
                    or (
                        incoming.message
                        and (incoming.message.chat.type == "private" or admin_context)
                    )
                )
                await enqueue(
                    conn,
                    "update",
                    f"update:{bot_id}:{incoming.update_id}",
                    {"id": str(iid)},
                    chat_id=gid,
                    priority=0 if high else 5,
                )
        if updates and accepted_new:
            await conn.execute(
                insert(t.cursors)
                .values(bot_id=bot_id, next_offset=updates[-1].update_id + 1)
                .on_conflict_do_update(
                    index_elements=["bot_id"],
                    set_={"next_offset": updates[-1].update_id + 1},
                )
            )


class Queue:
    def __init__(self, engine):
        self.engine = engine

    async def claim(self, control=None):
        async with self.engine.begin() as conn:
            # Serialize external effects per group. Checks are independently
            # version-fenced and may overlap; a hot group must use LLM capacity.
            if control is not True:
                await conn.execute(text("SELECT pg_advisory_xact_lock(71392143)"))
            await conn.execute(
                t.jobs.update()
                .where(t.jobs.c.state == "running", t.jobs.c.lease_until < now())
                .values(state="pending", lease_token=None, lease_until=None)
            )
            running = t.jobs.alias("running_job")
            running_lane = (
                select(running.c.id)
                .where(
                    running.c.state == "running",
                    running.c.chat_id == t.jobs.c.chat_id,
                    running.c.kind == t.jobs.c.kind,
                )
                .correlate(t.jobs)
                .exists()
            )
            query = (
                select(t.jobs)
                .outerjoin(
                    t.group_settings, t.group_settings.c.chat_id == t.jobs.c.chat_id
                )
                .where(
                    t.jobs.c.state == "pending",
                    t.jobs.c.due_at <= now(),
                    or_(
                        t.jobs.c.kind.in_(["update", "check"]),
                        t.jobs.c.chat_id.is_(None),
                        ~running_lane,
                    ),
                )
                .order_by(
                    t.jobs.c.priority,
                    t.group_settings.c.last_dispatched_at.asc().nullsfirst(),
                    t.jobs.c.due_at,
                    t.jobs.c.created_at,
                )
                .limit(1)
                .with_for_update(skip_locked=True, of=t.jobs)
            )
            if control is not None:
                query = query.where(
                    t.jobs.c.kind == "update" if control else t.jobs.c.kind != "update"
                )
            if control:
                query = query.order_by(None).order_by(
                    t.jobs.c.priority, t.jobs.c.due_at, t.jobs.c.created_at
                )
            row = (await conn.execute(query)).mappings().first()
            if not row:
                return None
            token = uuid.uuid4()
            await conn.execute(
                t.jobs.update()
                .where(t.jobs.c.id == row["id"])
                .values(
                    state="running",
                    attempt=row["attempt"] + 1,
                    lease_token=token,
                    lease_until=now() + timedelta(seconds=60),
                )
            )
            if row["chat_id"] and row["kind"] != "update":
                await conn.execute(
                    t.group_settings.update()
                    .where(t.group_settings.c.chat_id == row["chat_id"])
                    .values(last_dispatched_at=now())
                )
            return {**row, "lease_token": token, "attempt": row["attempt"] + 1}

    async def heartbeat(self, job):
        while True:
            await asyncio.sleep(15)
            async with self.engine.begin() as conn:
                changed = await conn.execute(
                    t.jobs.update()
                    .where(
                        t.jobs.c.id == job["id"],
                        t.jobs.c.lease_token == job["lease_token"],
                        t.jobs.c.state == "running",
                    )
                    .values(lease_until=now() + timedelta(seconds=60))
                )
                if not changed.rowcount:
                    return

    async def finish(
        self, job, *, error=None, delay=0, permanent=False, deferred=False
    ):
        async with self.engine.begin() as conn:
            state = (
                "done"
                if error is None
                else (
                    "dead"
                    if not deferred and (permanent or job["attempt"] >= 3)
                    else "pending"
                )
            )
            await conn.execute(
                t.jobs.update()
                .where(
                    t.jobs.c.id == job["id"], t.jobs.c.lease_token == job["lease_token"]
                )
                .values(
                    state=state,
                    last_error=error,
                    lease_token=None,
                    lease_until=None,
                    due_at=now() + timedelta(seconds=delay),
                )
            )

    async def retry(self, conn, chat_id, job_id):
        await conn.execute(
            t.jobs.update()
            .where(
                t.jobs.c.id == job_id,
                t.jobs.c.chat_id == chat_id,
                t.jobs.c.state == "dead",
            )
            .values(state="pending", attempt=0, due_at=now(), last_error=None)
        )
