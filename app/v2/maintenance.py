import asyncio
import contextlib
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, func

from app.v2 import tables as t
from app.v2.domain import Policy, now
from app.v2.queue import emit
from app.v2.state import expire_trust, audit
from app.v2.finance import global_state

LOG = logging.getLogger("antiscam.maintenance")


class Maintenance:
    def __init__(self, engine, bot, settings):
        self.engine, self.bot, self.settings = engine, bot, settings

    async def tick(self):
        async with self.engine.begin() as conn:
            # Keep the receipt/idempotency key, remove only old processed raw data.
            await conn.execute(
                t.inbox.update()
                .where(
                    t.inbox.c.state == "done",
                    t.inbox.c.created_at < now() - timedelta(days=7),
                    t.inbox.c.payload != {},
                )
                .values(payload={})
            )
            owners = (
                (
                    await conn.execute(
                        select(t.roles.c.actor_id).where(
                            t.roles.c.role == "owner",
                            t.roles.c.revoked_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            used = (
                await conn.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                func.coalesce(t.budget.c.spent, t.budget.c.reserved)
                            ),
                            0,
                        )
                    ).where(
                        t.budget.c.day == now().date().isoformat(),
                    )
                )
            ).scalar_one()
            oldest_due = (
                await conn.execute(
                    select(func.min(t.jobs.c.due_at)).where(
                        t.jobs.c.state == "pending",
                        t.jobs.c.due_at <= now(),
                    )
                )
            ).scalar_one()
            backlog_seconds = (now() - oldest_due).total_seconds() if oldest_due else 0
            backlog_level = "critical" if backlog_seconds >= 300 else "warning"
            global_budget = await global_state(
                conn, self.settings.global_daily_budget_usd
            )
            global_limit = global_budget["amount"]
            for uid in owners:
                if global_limit is not None and used >= global_limit * 0.8:
                    await emit(
                        conn,
                        destination=uid,
                        kind="budget",
                        payload={
                            "text": f"Общий бюджет: использовано/зарезервировано ${used:.4f} из ${global_limit:.4f}."
                        },
                        key=f"globalbudget80:{uid}:{now().date()}:{global_budget['version']}",
                    )
                if backlog_seconds >= 60:
                    await emit(
                        conn,
                        destination=uid,
                        kind="health",
                        payload={
                            "text": f"Очередь: {'критическая задержка' if backlog_level == 'critical' else 'предупреждение'}. Старейшее готовое задание ожидает {int(backlog_seconds)} с. Пороги: предупреждение 60 с, критический 300 с. Проверьте статус групп и журнал задач."
                        },
                        key=f"backlog:{backlog_level}:{uid}:{now().strftime('%Y-%m-%d-%H')}",
                    )
        async with self.engine.connect() as conn:
            groups = [
                dict(r)
                for r in (
                    await conn.execute(
                        select(
                            t.group_settings,
                            t.chats.c.telegram_chat_id,
                            t.bindings.c.admin_chat_id,
                            t.bindings.c.version.label("binding_version"),
                        )
                        .join(t.chats, t.chats.c.id == t.group_settings.c.chat_id)
                        .join(
                            t.bindings,
                            t.bindings.c.chat_id == t.group_settings.c.chat_id,
                        )
                        .where(
                            t.group_settings.c.lifecycle == "connected",
                            t.bindings.c.closed_at.is_(None),
                        )
                    )
                ).mappings()
            ]
        for group in groups:
            policy = Policy.model_validate(group["policy"])
            health, reason = "healthy", None
            try:
                member = await self.bot.get_chat_member(
                    group["telegram_chat_id"], self.bot.id
                )
                if (
                    str(member.status) != "administrator"
                    or not member.can_delete_messages
                    or not member.can_restrict_members
                ):
                    health, reason = "degraded", "work_permissions_missing"
                admin = await self.bot.get_chat_member(
                    group["admin_chat_id"], self.bot.id
                )
                if str(admin.status) != "administrator":
                    health, reason = "degraded", "admin_unavailable"
            except Exception:
                health, reason = "degraded", "telegram_health_unknown"
            if health == "healthy":
                async with self.engine.connect() as conn:
                    provider = (
                        await conn.execute(
                            select(t.checks.c.status)
                            .where(
                                t.checks.c.chat_id == group["chat_id"],
                                t.checks.c.source == "live",
                                (t.checks.c.status == "ok")
                                | t.checks.c.error.like("provider_%"),
                            )
                            .order_by(t.checks.c.created_at.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                if provider == "error":
                    health, reason = "degraded", "provider_unavailable"
            async with self.engine.begin() as conn:
                current = (
                    (
                        await conn.execute(
                            select(t.group_settings)
                            .where(t.group_settings.c.chat_id == group["chat_id"])
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                if current["health"] != health or current["health_reason"] != reason:
                    version = current["version"] + 1
                    await conn.execute(
                        t.group_settings.update()
                        .where(t.group_settings.c.chat_id == group["chat_id"])
                        .values(
                            health=health,
                            health_reason=reason,
                            version=version,
                            updated_at=now(),
                        )
                    )
                    await conn.execute(
                        t.actions.update()
                        .where(
                            t.actions.c.chat_id == group["chat_id"],
                            t.actions.c.state == "planned",
                        )
                        .values(state="cancelled", error="health_changed")
                    )
                    await audit(
                        conn,
                        group["chat_id"],
                        None,
                        "health.changed",
                        health=health,
                        reason=reason,
                    )
                    await emit(
                        conn,
                        destination=group["admin_chat_id"],
                        kind="health",
                        payload={
                            "text": f"Группа {group['chat_id']}: {health}. Причина: {reason or 'права восстановлены'}. Режим модерации сохранён."
                        },
                        key=f"health:{group['chat_id']}:{version}",
                        chat_id=group["chat_id"],
                        binding_version=group["binding_version"],
                    )
                    if health != "healthy":
                        owners = (
                            (
                                await conn.execute(
                                    select(t.roles.c.actor_id).where(
                                        t.roles.c.role == "owner",
                                        t.roles.c.revoked_at.is_(None),
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        for uid in owners:
                            await emit(
                                conn,
                                destination=uid,
                                kind="health",
                                payload={
                                    "text": f"Группа {group['chat_id']} требует внимания: {reason}."
                                },
                                key=f"ownerhealth:{uid}:{group['chat_id']}:{version}",
                            )
                expired = (
                    (
                        await conn.execute(
                            select(t.trust.c.user_id)
                            .where(
                                t.trust.c.chat_id == group["chat_id"],
                                t.trust.c.revoked_at.is_(None),
                                t.trust.c.expires_at <= now(),
                            )
                            .limit(100)
                        )
                    )
                    .scalars()
                    .all()
                )
                for uid in expired:
                    await expire_trust(conn, group["chat_id"], uid)
                local = now().astimezone(ZoneInfo(policy.timezone))
                if (
                    policy.daily_summary_time
                    and local.strftime("%H:%M") >= policy.daily_summary_time
                ):
                    counts = (
                        await conn.execute(
                            select(t.incidents.c.status, func.count())
                            .where(
                                t.incidents.c.chat_id == group["chat_id"],
                                t.incidents.c.created_at > now() - timedelta(days=1),
                            )
                            .group_by(t.incidents.c.status)
                        )
                    ).all()
                    await emit(
                        conn,
                        destination=group["admin_chat_id"],
                        kind="summary",
                        payload={
                            "text": f"Сводка группы {group['chat_id']} за 24 часа\n"
                            + "\n".join(f"{k}: {v}" for k, v in counts)
                        },
                        key=f"summary:{group['chat_id']}:{local.date()}",
                        chat_id=group["chat_id"],
                        binding_version=group["binding_version"],
                    )
                if policy.daily_budget_usd:
                    used = (
                        await conn.execute(
                            select(
                                func.coalesce(
                                    func.sum(
                                        func.coalesce(
                                            t.budget.c.spent, t.budget.c.reserved
                                        )
                                    ),
                                    0,
                                )
                            ).where(
                                t.budget.c.chat_id == group["chat_id"],
                                t.budget.c.day == now().date().isoformat(),
                            )
                        )
                    ).scalar_one()
                    if used >= policy.daily_budget_usd * 0.8:
                        for uid in owners:
                            await emit(
                                conn,
                                destination=uid,
                                kind="budget",
                                payload={
                                    "text": f"Группа {group['chat_id']}: использовано/зарезервировано ${used:.4f} из ${policy.daily_budget_usd:.4f}. При исчерпании лимита проверки останутся в очереди."
                                },
                                key=f"budget80:{group['chat_id']}:{uid}:{now().date()}",
                            )

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                LOG.warning("maintenance_error type=%s", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 60)
