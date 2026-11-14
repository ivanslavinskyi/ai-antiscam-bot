"""Single receiver, durable PostgreSQL inbox, independent bounded workers."""

import asyncio
import contextlib
import logging
import signal
import sys
import time
import uuid

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import Update
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramBadRequest,
    TelegramConflictError,
)
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.v2 import tables as t
from app.v2.actions import ActionExecutor
from app.v2.auth import Authorization
from app.v2.config import get_settings
from app.v2.content import OCR
from app.v2.db import engine_for, check_ready, check_bot_identity
from app.v2.delivery import Delivery
from app.v2.domain import now
from app.v2.llm import Classifier, Budget, RetryLater
from app.v2.onboarding import Onboarding
from app.v2.operator import Operator
from app.v2.pipeline import Pipeline
from app.v2.queue import Queue, persist_updates
from app.v2.ui import UI
from app.v2.controls import heartbeat
from app.v2.state import audit

LOG = logging.getLogger("antiscam")
ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "my_chat_member",
    "chat_member",
]


class Runtime:
    def __init__(self, engine, bot, settings):
        self.engine, self.bot, self.settings = engine, bot, settings
        auth = Authorization(engine, bot)
        self.onboarding = Onboarding(engine, bot, auth)
        self.ui = UI(engine, bot, auth, Operator(engine, auth), self.onboarding)
        self.ui.model_name = settings.openai_model
        self.ui.global_budget_default = settings.global_daily_budget_usd
        self.classifier = Classifier(
            settings, Budget(engine, settings.global_daily_budget_usd)
        )
        self.pipeline = Pipeline(
            engine,
            bot,
            self.classifier,
            OCR(
                bot,
                settings.ocr_concurrency,
                max_bytes=settings.ocr_max_bytes,
                max_pixels=settings.ocr_max_pixels,
                timeout=settings.ocr_timeout_seconds,
            ),
        )
        self.actions = ActionExecutor(engine, bot, auth, settings.recovery_mode)
        self.delivery = Delivery(engine, bot, self.onboarding)
        self.queue, self.stop = Queue(engine), asyncio.Event()
        self.next_worker_heartbeat = 0.0

    async def receive(self):
        # A dedicated session lock survives transactions but never connection loss.
        async with self.engine.connect() as lock:
            locked = (
                await lock.execute(
                    text("SELECT pg_try_advisory_lock(:bot)"), {"bot": self.bot.id}
                )
            ).scalar_one()
            await lock.commit()
            if not locked:
                raise RuntimeError(
                    "Another receiver owns this bot. Refusing duplicate polling."
                )
            try:
                while not self.stop.is_set():
                    # Losing this connection means losing ownership: abort, never transparently reconnect.
                    await lock.execute(text("SELECT 1"))
                    await lock.commit()
                    async with self.engine.connect() as conn:
                        offset = (
                            await conn.execute(
                                select(t.cursors.c.next_offset).where(
                                    t.cursors.c.bot_id == self.bot.id
                                )
                            )
                        ).scalar_one_or_none()
                    try:
                        updates = await self.bot.get_updates(
                            offset=offset,
                            timeout=25,
                            limit=100,
                            allowed_updates=ALLOWED_UPDATES,
                        )
                    except TelegramRetryAfter as exc:
                        await self.pause(min(60, exc.retry_after))
                        continue
                    except TelegramConflictError:
                        raise RuntimeError(
                            "Telegram reports another active receiver. Stop the other instance before restarting."
                        ) from None
                    except Exception as exc:
                        LOG.warning("poll_error type=%s", type(exc).__name__)
                        await self.pause(5)
                        continue
                    await persist_updates(self.engine, self.bot.id, updates)
                    await heartbeat(self.engine, "receiver", bot_id=self.bot.id)
            finally:
                with contextlib.suppress(Exception):
                    await lock.execute(
                        text("SELECT pg_advisory_unlock(:bot)"), {"bot": self.bot.id}
                    )

    async def pause(self, seconds):
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), seconds)

    async def process_update(self, job):
        iid = uuid.UUID(job["payload"]["id"])
        async with self.engine.connect() as conn:
            row = (
                (await conn.execute(select(t.inbox).where(t.inbox.c.id == iid)))
                .mappings()
                .one()
            )
            if row["state"] == "done":
                return
        update = Update.model_validate(row["payload"])
        if update.callback_query:
            await self.ui.callback(update.callback_query)
        elif update.chat_member:
            event = update.chat_member
            async with self.engine.begin() as conn:
                gid = (
                    await conn.execute(
                        select(t.aliases.c.chat_id).where(
                            t.aliases.c.telegram_chat_id == event.chat.id
                        )
                    )
                ).scalar_one_or_none()
                if gid:
                    await audit(
                        conn,
                        gid,
                        event.from_user.id,
                        "restriction.observed",
                        user_id=event.new_chat_member.user.id,
                        status=str(event.new_chat_member.status),
                        telegram_date=event.date.isoformat(),
                        update_id=update.update_id,
                    )
        elif update.my_chat_member:
            event = update.my_chat_member
            async with self.engine.begin() as conn:
                stmt = insert(t.candidates).values(
                    telegram_chat_id=event.chat.id,
                    inviter_id=event.from_user.id,
                    title=event.chat.title,
                    updated_at=now(),
                )
                await conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["telegram_chat_id"],
                        set_={
                            "inviter_id": stmt.excluded.inviter_id,
                            "title": stmt.excluded.title,
                            "updated_at": now(),
                        },
                    )
                )
        elif update.message or update.edited_message:
            message = update.message or update.edited_message
            if message.migrate_to_chat_id or message.migrate_from_chat_id:
                await self.chat_migration(message)
            elif not await self.ui.message(message):
                await self.pipeline.ingest(message, update.update_id)
        async with self.engine.begin() as conn:
            await conn.execute(
                t.inbox.update().where(t.inbox.c.id == iid).values(state="done")
            )

    async def chat_migration(self, message):
        old = message.migrate_from_chat_id or message.chat.id
        new = message.migrate_to_chat_id or message.chat.id
        async with self.engine.begin() as conn:
            gid = (
                await conn.execute(
                    select(t.aliases.c.chat_id).where(
                        t.aliases.c.telegram_chat_id == old
                    )
                )
            ).scalar_one_or_none()
            if gid:
                await conn.execute(
                    select(t.group_settings)
                    .where(t.group_settings.c.chat_id == gid)
                    .with_for_update()
                )
                await conn.execute(
                    insert(t.aliases)
                    .values(telegram_chat_id=new, chat_id=gid)
                    .on_conflict_do_nothing()
                )
                await conn.execute(
                    t.chats.update()
                    .where(t.chats.c.id == gid)
                    .values(telegram_chat_id=new)
                )
            # Admin chat migration invalidates previous binding versions and queued actions.
            rows = (
                (
                    await conn.execute(
                        select(t.bindings).where(
                            t.bindings.c.admin_chat_id == old,
                            t.bindings.c.closed_at.is_(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
            for binding in rows:
                await conn.execute(
                    select(t.group_settings)
                    .where(t.group_settings.c.chat_id == binding["chat_id"])
                    .with_for_update()
                )
                await conn.execute(
                    t.bindings.update()
                    .where(t.bindings.c.id == binding["id"])
                    .values(closed_at=now())
                )
                await conn.execute(
                    t.bindings.insert().values(
                        chat_id=binding["chat_id"],
                        admin_chat_id=new,
                        version=binding["version"] + 1,
                        confirmed_by=binding["confirmed_by"],
                    )
                )

    async def worker(self, number):
        while not self.stop.is_set():
            job = await self.queue.claim(control=number == 0)
            if not job:
                await self.pause(0.25)
                continue
            lease_task = asyncio.create_task(self.queue.heartbeat(job))
            try:
                handler = {
                    "update": self.process_update,
                    "check": self.pipeline.check,
                    "action": self.actions.execute,
                    "outbox": self.delivery.execute,
                }.get(job["kind"])
                if self.settings.recovery_mode and job["kind"] in {
                    "action",
                    "outbox",
                    "notice_cleanup",
                }:
                    raise RetryLater("recovery_mode", 60)
                if handler:
                    await handler(job)
                elif job["kind"] == "notice_cleanup":
                    try:
                        await self.bot.delete_message(**job["payload"])
                    except TelegramBadRequest:
                        pass
                else:
                    raise ValueError("Unknown job kind")
            except RetryLater as exc:
                await self.queue.finish(
                    job, error=exc.code, delay=exc.delay, deferred=True
                )
            except asyncio.CancelledError:
                # Lease expires; in_flight external actions are reconciled, never blindly replayed.
                raise
            except Exception as exc:
                LOG.error(
                    "job_error id=%s kind=%s type=%s",
                    job["id"],
                    job["kind"],
                    type(exc).__name__,
                )
                await self.queue.finish(
                    job, error=type(exc).__name__, delay=min(60, job["attempt"] * 10)
                )
            else:
                await self.queue.finish(job)
                if time.monotonic() >= self.next_worker_heartbeat:
                    # One process heartbeat per interval, not a contended DB write per job.
                    self.next_worker_heartbeat = time.monotonic() + 10
                    await heartbeat(self.engine, "worker", job_kind=job["kind"])
            finally:
                lease_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await lease_task

    async def run(self):
        await check_ready(self.engine)
        me = await self.bot.get_me()
        await check_bot_identity(self.engine, me.id)
        webhook = await self.bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError(
                "Webhook is configured. Explicitly prepare polling before starting V2."
            )
        from app.v2.maintenance import Maintenance

        maintenance = Maintenance(self.engine, self.bot, self.settings)
        tasks = [asyncio.create_task(self.receive())]
        tasks += [
            asyncio.create_task(self.worker(i))
            for i in range(self.settings.worker_count)
        ]
        tasks.append(asyncio.create_task(maintenance.run(self.stop)))
        stopper = asyncio.create_task(self.stop.wait())
        try:
            done, _ = await asyncio.wait(
                [*tasks, stopper], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not stopper:
                    task.result()
        finally:
            self.stop.set()
            # Workers can finish a bounded network operation before cancellation.
            _, pending = await asyncio.wait(tasks, timeout=30)
            for task in pending:
                task.cancel()
            stopper.cancel()
            await asyncio.gather(*tasks, stopper, return_exceptions=True)
            await self.classifier.close()
            await self.bot.session.close()


async def main():
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    engine = engine_for(settings.db_url.get_secret_value())
    bot = Bot(
        settings.telegram_bot_token.get_secret_value(),
        session=AiohttpSession(timeout=30),
    )
    runtime = Runtime(engine, bot, settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, runtime.stop.set)
    try:
        await runtime.run()
    finally:
        await engine.dispose()


def entrypoint():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())


if __name__ == "__main__":
    entrypoint()
