"""Real PostgreSQL load harness with bounded, delayed Telegram/LLM fakes.

Requires an EMPTY, isolated local antiscam_test_* database. Does not load .env or
access Telegram/OpenAI. Reports actual duration; short smoke runs are not acceptance.
"""

import argparse
import asyncio
from collections import Counter, defaultdict, deque
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from aiogram.types import Message, Update, CallbackQuery, ChatPermissions
from alembic import command
from alembic.config import Config
from sqlalchemy import select, func
from sqlalchemy.engine import make_url

from app.v2 import tables as t
from app.v2.config import Settings
from app.v2.db import engine_for
from app.v2.domain import Policy, now
from app.v2.llm import Verdict
from app.v2.queue import persist_updates
from app.v2.runtime import Runtime
from app.v2.maintenance import Maintenance


def rss_mb():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        current = ctypes.windll.kernel32.GetCurrentProcess
        current.restype = wintypes.HANDLE
        data = Counters()
        data.cb = ctypes.sizeof(data)
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        get_memory.restype = wintypes.BOOL
        if not get_memory(current(), ctypes.byref(data), data.cb):
            return None
        return data.WorkingSetSize / 1024**2
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


class FakeBot:
    id = 999001

    def __init__(self, delay):
        self.delay = delay
        self.calls = Counter()
        self.pending_callbacks = {}
        self.callback_latencies = []
        self.pending_commands = defaultdict(deque)
        self.command_latencies = []
        self.message_id = 1

    async def pause(self, method):
        self.calls[method] += 1
        await asyncio.sleep(self.delay)

    async def get_chat_member(self, chat, user):
        await self.pause("get_chat_member")
        values = {
            "status": "administrator" if user in {self.id, 111} else "member",
            "can_delete_messages": True,
            "can_restrict_members": True,
            "is_member": True,
        }
        return SimpleNamespace(**values, model_dump=lambda **_: values)

    async def get_chat(self, chat):
        await self.pause("get_chat")
        return SimpleNamespace(
            id=chat,
            type="supergroup",
            title="Load group",
            permissions=ChatPermissions(can_send_messages=True),
        )

    async def send_message(self, *args, **kwargs):
        await self.pause("send_message")
        chat = kwargs.get("chat_id", args[0] if args else None)
        text = kwargs.get("text", args[1] if len(args) > 1 else "")
        if (
            "Действия и списки относятся только к этой рабочей группе." in text
            and self.pending_commands[chat]
        ):
            self.command_latencies.append(
                time.monotonic() - self.pending_commands[chat].popleft()
            )
        self.message_id += 1
        return SimpleNamespace(message_id=self.message_id)

    async def answer_callback_query(self, query, *args, **kwargs):
        await self.pause("answer_callback_query")
        started = self.pending_callbacks.pop(query, None)
        if started is not None:
            self.callback_latencies.append(time.monotonic() - started)

    async def delete_message(self, *args, **kwargs):
        await self.pause("delete_message")
        return True

    async def edit_message_text(self, *args, **kwargs):
        await self.pause("edit_message_text")
        return True

    async def ban_chat_member(self, *args, **kwargs):
        await self.pause("ban_chat_member")
        return True

    async def restrict_chat_member(self, *args, **kwargs):
        await self.pause("restrict_chat_member")
        return True


class FakeClassifier:
    settings = SimpleNamespace(openai_model="gpt-4o-mini")

    def __init__(self, delay):
        self.delay = delay
        self.semaphore = asyncio.Semaphore(2)

    async def classify(self, gid, key, content, *args):
        async with self.semaphore:
            await asyncio.sleep(self.delay)
        label = (
            "SCAM"
            if any("SCAM" in text for text in content["own_publication"])
            else "OK"
        )
        return Verdict(
            label=label,
            category="other",
            confidence=0.99,
            reason="Synthetic load fixture",
        ), {}


async def run(args, url):
    engine = engine_for(url)
    async with engine.begin() as conn:
        if (await conn.execute(select(func.count()).select_from(t.chats))).scalar_one():
            raise RuntimeError(
                "Load database must be empty; no existing data is deleted"
            )
        for gid in range(1, args.groups + 1):
            await conn.execute(
                t.chats.insert().values(
                    id=gid,
                    telegram_chat_id=-100000 - gid,
                    title=f"Load {gid}",
                    type="supergroup",
                    is_active=True,
                )
            )
            await conn.execute(
                t.aliases.insert().values(chat_id=gid, telegram_chat_id=-100000 - gid)
            )
            await conn.execute(
                t.group_settings.insert().values(
                    chat_id=gid,
                    lifecycle="connected",
                    mode="auto",
                    policy=Policy().model_dump(),
                )
            )
            await conn.execute(
                t.bindings.insert().values(
                    chat_id=gid,
                    admin_chat_id=-200000 - gid,
                    version=1,
                    confirmed_by=111,
                )
            )
        await conn.execute(
            t.roles.insert().values(actor_id=111, role="owner", granted_by=111)
        )
    bot = FakeBot(args.telegram_delay)
    settings = Settings(
        _env_file=None,
        db_url=url,
        telegram_bot_token="999001:FAKE",
        openai_api_key="",
        worker_count=args.workers,
    )
    runtime = Runtime(engine, bot, settings)
    await runtime.classifier.close()
    runtime.pipeline.classifier = FakeClassifier(args.llm_delay)
    workers = [
        asyncio.create_task(runtime.worker(i)) for i in range(settings.worker_count)
    ]
    maintainer = asyncio.create_task(
        Maintenance(engine, bot, settings).run(runtime.stop)
    )
    started = time.monotonic()
    produced = 0
    publications = 0
    samples = []

    async def sample():
        while not runtime.stop.is_set():
            async with engine.connect() as conn:
                pending = (
                    await conn.execute(
                        select(func.count())
                        .select_from(t.jobs)
                        .where(t.jobs.c.state.in_(["pending", "running"]))
                    )
                ).scalar_one()
            value = {
                "seconds": round(time.monotonic() - started),
                "produced": produced,
                "pending_jobs": pending,
                "rss_mb": rss_mb(),
            }
            samples.append(value)
            print(json.dumps(value), flush=True)
            await runtime.pause(60)

    sampler = asyncio.create_task(sample())
    try:
        for duration, rate in [
            (args.duration, args.rate),
            (args.burst_seconds, args.burst_rate),
        ]:
            phase = time.monotonic()
            for n in range(int(duration * rate)):
                await asyncio.sleep(max(0, phase + n / rate - time.monotonic()))
                produced += 1
                publications += 1
                gid = 1 if n % 10 < 6 else 2 + (n // 10) % (args.groups - 1)
                publication = Message(
                    message_id=publications,
                    date=now(),
                    chat={"id": -100000 - gid, "type": "supergroup"},
                    from_user={
                        "id": 900000 + publications,
                        "is_bot": False,
                        "first_name": "Synthetic",
                    },
                    text="SCAM" if n % 5 == 0 else "Ordinary discussion",
                )
                await persist_updates(
                    engine, bot.id, [Update(update_id=produced, message=publication)]
                )
                if n % int(rate * 10) == 0:
                    produced += 1
                    qid = "load-" + str(produced)
                    bot.pending_callbacks[qid] = time.monotonic()
                    query = CallbackQuery(
                        id=qid,
                        from_user={"id": 111, "is_bot": False, "first_name": "Owner"},
                        chat_instance="fixture",
                        message=publication.model_copy(
                            update={
                                "chat": publication.chat.model_copy(
                                    update={"id": -200000 - gid}
                                )
                            }
                        ),
                        data=f"v:status:{gid}",
                    )
                    await persist_updates(
                        engine,
                        bot.id,
                        [Update(update_id=produced, callback_query=query)],
                    )
                    produced += 1
                    admin = -200000 - gid
                    bot.pending_commands[admin].append(time.monotonic())
                    command = publication.model_copy(
                        update={
                            "message_id": 10_000_000 + produced,
                            "text": "/as",
                            "chat": publication.chat.model_copy(update={"id": admin}),
                            "from_user": query.from_user,
                        }
                    )
                    await persist_updates(
                        engine, bot.id, [Update(update_id=produced, message=command)]
                    )
        drain_started = time.monotonic()
        while time.monotonic() - drain_started < 180:
            async with engine.connect() as conn:
                pending = (
                    await conn.execute(
                        select(func.count())
                        .select_from(t.jobs)
                        .where(
                            t.jobs.c.state.in_(["pending", "running"]),
                            t.jobs.c.kind != "notice_cleanup",
                        )
                    )
                ).scalar_one()
            if not pending:
                break
            await asyncio.sleep(1)
        async with engine.connect() as conn:
            received = (
                await conn.execute(select(func.count()).select_from(t.inbox))
            ).scalar_one()
            done = (
                await conn.execute(
                    select(func.count())
                    .select_from(t.inbox)
                    .where(t.inbox.c.state == "done")
                )
            ).scalar_one()
            incidents = (
                await conn.execute(select(func.count()).select_from(t.incidents))
            ).scalar_one()
            dead = (
                await conn.execute(
                    select(func.count())
                    .select_from(t.jobs)
                    .where(t.jobs.c.state == "dead")
                )
            ).scalar_one()
            duplicate_strikes = (
                await conn.execute(
                    select(func.count()).select_from(
                        select(t.strikes.c.incident_id)
                        .where(t.strikes.c.kind == "incident")
                        .group_by(t.strikes.c.incident_id)
                        .having(func.count() > 1)
                        .subquery()
                    )
                )
            ).scalar_one()
            wait_seconds = func.extract(
                "epoch", t.checks.c.created_at - t.items.c.telegram_date
            )
            group_timings = [
                {
                    "group": r[0],
                    "checks": r[1],
                    "p95_seconds": float(r[2]),
                    "max_seconds": float(r[3]),
                }
                for r in (
                    await conn.execute(
                        select(
                            t.checks.c.chat_id,
                            func.count(),
                            func.percentile_cont(0.95).within_group(wait_seconds),
                            func.max(wait_seconds),
                        )
                        .join(t.revisions, t.revisions.c.id == t.checks.c.revision_id)
                        .join(t.items, t.items.c.id == t.revisions.c.item_id)
                        .group_by(t.checks.c.chat_id)
                        .order_by(t.checks.c.chat_id)
                    )
                ).all()
            ]
        latency = sorted(bot.callback_latencies)
        p95 = (
            latency[min(len(latency) - 1, int(len(latency) * 0.95))]
            if latency
            else None
        )
        command_latency = sorted(bot.command_latencies)
        command_p95 = (
            command_latency[
                min(len(command_latency) - 1, int(len(command_latency) * 0.95))
            ]
            if command_latency
            else None
        )
        report = {
            "configuration": vars(args) | {"output": str(args.output)},
            "duration_seconds": time.monotonic() - started,
            "received": received,
            "produced": produced,
            "processed": done,
            "publications": publications,
            "incidents": incidents,
            "dead_jobs": dead,
            "pending_jobs": pending,
            "duplicate_strikes": duplicate_strikes,
            "callback_p95_seconds": p95,
            "callback_count": len(latency),
            "command_p95_seconds": command_p95,
            "command_count": len(command_latency),
            "group_timings": group_timings,
            "samples": samples,
            "fake_api_calls": dict(bot.calls),
            "real_api_calls": False,
            "background_maintenance": True,
            "acceptance_duration": args.duration >= 1800 and args.burst_seconds >= 60,
        }
        report["passed"] = (
            received == produced == done
            and publications == incidents
            and not dead
            and not pending
            and not duplicate_strikes
            and p95 is not None
            and p95 <= 2
            and command_p95 is not None
            and command_p95 <= 2
            and not any(bot.pending_commands.values())
            and len(group_timings) == args.groups
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    k: v
                    for k, v in report.items()
                    if k
                    not in {
                        "samples",
                        "configuration",
                        "fake_api_calls",
                        "group_timings",
                    }
                }
            ),
            flush=True,
        )
    finally:
        runtime.stop.set()
        await asyncio.gather(*workers, sampler, maintainer, return_exceptions=True)
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=1800)
    parser.add_argument("--rate", type=float, default=10)
    parser.add_argument("--burst-seconds", type=int, default=60)
    parser.add_argument("--burst-rate", type=float, default=20)
    parser.add_argument("--groups", type=int, default=50)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--telegram-delay", type=float, default=0.02)
    parser.add_argument("--llm-delay", type=float, default=0.05)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (
        args.groups < 2
        or not 2 <= args.workers <= 32
        or min(args.duration, args.rate, args.burst_rate, args.burst_seconds) <= 0
    ):
        parser.error("Positive rates/durations and at least two groups are required")
    url = os.environ["ANTISCAM_TEST_DATABASE_URL"]
    parsed = make_url(url)
    if parsed.host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    } or not parsed.database.startswith("antiscam_test_"):
        parser.error("Only isolated local antiscam_test_* databases are permitted")
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, "head")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run(args, url))


if __name__ == "__main__":
    main()
