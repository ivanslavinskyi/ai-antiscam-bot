import asyncio

from aiogram.types import Update
from sqlalchemy import select, func

from app.v2 import tables as t
from app.v2.config import Settings
from app.v2.runtime import Runtime
from app.v2.queue import persist_updates
from conftest import message
from test_pipeline import Classifier


async def test_worker_remains_alive_after_multiple_jobs(db, bot, test_url):
    runtime = Runtime(db, bot, Settings(_env_file=None, db_url=test_url))
    await runtime.classifier.close()
    runtime.pipeline.classifier = Classifier()
    tasks = [asyncio.create_task(runtime.worker(i)) for i in range(4)]
    try:
        await persist_updates(
            db, bot.id, [Update(update_id=i, message=message(i)) for i in range(1, 4)]
        )
        async with asyncio.timeout(15):
            while True:
                for task in tasks:
                    assert not task.done(), (
                        repr(task.exception()) if task.done() else ""
                    )
                async with db.connect() as conn:
                    count = (
                        await conn.execute(
                            select(func.count())
                            .select_from(t.incidents)
                            .where(t.incidents.c.status == "checked")
                        )
                    ).scalar_one()
                if count == 3:
                    break
                await asyncio.sleep(0.1)
        async with db.connect() as conn:
            assert (
                await conn.execute(
                    select(func.count())
                    .select_from(t.inbox)
                    .where(t.inbox.c.state == "done")
                )
            ).scalar_one() == 3
    finally:
        runtime.stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
