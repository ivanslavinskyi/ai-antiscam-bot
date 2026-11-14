"""Read-only readiness report; Telegram is accessed only with --telegram."""

import argparse
import asyncio
import json
import shutil
import sys
import subprocess
import os

from sqlalchemy import text, select
from app.v2.config import get_settings
from app.v2.db import engine_for, check_ready, check_bot_identity
from app.v2 import tables as t


async def inspect(telegram=False):
    settings = get_settings()
    engine = engine_for(settings.db_url.get_secret_value())
    report = {
        "python": sys.version.split()[0],
        "recovery_mode": settings.recovery_mode,
        "model": settings.openai_model,
        "checks": {},
    }
    try:
        await check_ready(engine)
        report["checks"]["schema_and_import"] = True
        async with engine.connect() as conn:
            report["postgresql_version"] = (
                await conn.execute(text("SHOW server_version"))
            ).scalar_one()
            version_number = int(
                (await conn.execute(text("SHOW server_version_num"))).scalar_one()
            )
            report["checks"]["postgresql_target_17"] = 170012 <= version_number < 180000
            report["checks"]["nonroot_linux"] = (
                sys.platform == "linux" and os.geteuid() != 0
            )
            report["groups"] = [
                dict(r)
                for r in (
                    await conn.execute(
                        select(
                            t.group_settings.c.chat_id,
                            t.group_settings.c.lifecycle,
                            t.group_settings.c.mode,
                            t.group_settings.c.health,
                        )
                    )
                ).mappings()
            ]
            owners = (
                (
                    await conn.execute(
                        select(t.roles.c.actor_id).where(
                            t.roles.c.role == "owner", t.roles.c.revoked_at.is_(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
            report["checks"]["owners_present"] = bool(owners)
        executable = shutil.which("tesseract")
        report["checks"]["ocr_languages"] = False
        if executable:
            process = await asyncio.to_thread(
                subprocess.run,
                [executable, "--list-langs"],
                capture_output=True,
                timeout=10,
                check=True,
            )
            output = process.stdout
            report["checks"]["ocr_languages"] = {"rus", "ukr", "eng"} <= set(
                output.decode().split()
            )
        if telegram:
            from aiogram import Bot

            bot = Bot(settings.telegram_bot_token.get_secret_value())
            try:
                me = await bot.get_me()
                report["bot_id"] = me.id
                await check_bot_identity(engine, me.id)
                report["checks"]["bot_identity_matches_import"] = True
                report["checks"]["polling_no_webhook"] = not bool(
                    (await bot.get_webhook_info()).url
                )
                async with engine.connect() as conn:
                    rows = (
                        await conn.execute(
                            select(
                                t.chats.c.telegram_chat_id, t.bindings.c.admin_chat_id
                            )
                            .join(t.bindings, t.bindings.c.chat_id == t.chats.c.id)
                            .where(t.bindings.c.closed_at.is_(None))
                        )
                    ).all()
                for work, admin in rows:
                    work_member = await bot.get_chat_member(work, me.id)
                    admin_member = await bot.get_chat_member(admin, me.id)
                    report["checks"][f"telegram_rights_{work}"] = (
                        str(work_member.status) == "administrator"
                        and work_member.can_delete_messages
                        and work_member.can_restrict_members
                        and str(admin_member.status) == "administrator"
                    )
            finally:
                await bot.session.close()
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["checks"]["completed"] = False
    finally:
        await engine.dispose()
    report["ready"] = all(report["checks"].values()) and report["python"].startswith(
        "3.12."
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Read-only Telegram identity/permissions checks",
    )
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(inspect(args.telegram)))


if __name__ == "__main__":
    main()
