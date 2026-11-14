"""Telegram is not transactional. Persist intent before I/O, reconcile uncertainty."""

import uuid
from datetime import timedelta

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import ChatPermissions, BufferedInputFile, MessageEntity, User
from sqlalchemy import select

from app.v2 import tables as t
from app.v2.domain import Denied, Conflict, Policy, now
from app.v2.llm import RetryLater
from app.v2.state import current_binding, audit, active_list, plan_action
from app.v2.controls import flags


def permissions(data):
    return {
        k: bool(v)
        for k, v in data.items()
        if k in ChatPermissions.model_fields and v is not None
    }


def warning_text(action, user):
    deleted = action["payload"].get("deletion_notice")
    if deleted:
        reason = (
            "сообщение удалено модератором."
            if action["actor_id"]
            else "сообщение удалено по правилам группы."
        )
    else:
        reason = "в сообщении обнаружены признаки скама."
    prefix = "⚠️ "
    if not user:
        return prefix + reason[0].upper() + reason[1:], []
    name = " ".join(filter(None, (user.get("first_name"), user.get("last_name"))))
    name = " ".join(name.split())[:120] or user.get("username") or "Участник"
    entity = MessageEntity(
        type="text_mention",
        offset=len(prefix.encode("utf-16-le")) // 2,
        length=len(name.encode("utf-16-le")) // 2,
        user=User(id=user["telegram_user_id"], is_bot=False, first_name=name),
    )
    return f"{prefix}{name}, {reason}", [entity]


class ActionExecutor:
    def __init__(self, engine, bot, auth, recovery=False):
        self.engine, self.bot, self.auth, self.recovery = engine, bot, auth, recovery

    async def blacklist_blocks_release(self, conn, action):
        current = await active_list(conn, t.deny, action["chat_id"], action["user_id"])
        if not current:
            return False
        return not (
            action["actor_id"]
            and action["payload"].get("acknowledged_deny_id") == str(current["id"])
        )

    async def set_state(self, aid, state, error=None, **values):
        async with self.engine.begin() as conn:
            action = (
                (
                    await conn.execute(
                        select(t.actions).where(t.actions.c.id == aid).with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            await conn.execute(
                t.actions.update()
                .where(t.actions.c.id == aid)
                .values(state=state, error=error, updated_at=now(), **values)
            )
            await audit(
                conn,
                action["chat_id"],
                action["actor_id"],
                "action." + state,
                action_id=str(aid),
                action_kind=action["kind"],
                error=error,
            )
            if action["kind"] == "delete" and state == "succeeded":
                await self.plan_deletion_notice(conn, action)
            if action["incident_id"] and state in {
                "succeeded",
                "failed",
                "unknown",
                "cancelled",
            }:
                from app.v2.pipeline import card

                incident = (
                    (
                        await conn.execute(
                            select(t.incidents).where(
                                t.incidents.c.id == action["incident_id"],
                                t.incidents.c.chat_id == action["chat_id"],
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                await card(conn, incident, suffix=f"action:{aid}:{state}")

    async def plan_deletion_notice(self, conn, action):
        """One notice per publication revision, only after confirmed deletion."""
        if not action["incident_id"]:
            return
        existing = (
            await conn.execute(
                select(t.actions.c.id)
                .where(
                    t.actions.c.chat_id == action["chat_id"],
                    t.actions.c.incident_id == action["incident_id"],
                    t.actions.c.object_version == action["object_version"],
                    t.actions.c.kind == "warn",
                    t.actions.c.state != "cancelled",
                )
                .limit(1)
            )
        ).first()
        if existing:
            return
        settings = (
            (
                await conn.execute(
                    select(t.group_settings).where(
                        t.group_settings.c.chat_id == action["chat_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
        binding = await current_binding(conn, action["chat_id"])
        if (
            not binding
            or binding["version"] != action["binding_version"]
            or settings["version"] != action["policy_version"]
            or settings["lifecycle"] != "connected"
        ):
            return
        await plan_action(
            conn,
            settings,
            "warn",
            {"deletion_notice": True},
            f"delete-notice:{action['incident_id']}:{action['object_version']}",
            incident={"id": action["incident_id"], "version": action["object_version"]},
            user_id=action["user_id"],
            actor=action["actor_id"],
        )

    async def execute(self, job):
        aid = uuid.UUID(job["payload"]["id"])
        async with self.engine.connect() as conn:
            action = (
                (await conn.execute(select(t.actions).where(t.actions.c.id == aid)))
                .mappings()
                .one()
            )
            if action["state"] in {"succeeded", "cancelled", "failed"}:
                return
            settings = (
                (
                    await conn.execute(
                        select(t.group_settings).where(
                            t.group_settings.c.chat_id == action["chat_id"]
                        )
                    )
                )
                .mappings()
                .one()
            )
            binding = await current_binding(conn, action["chat_id"])
            tgchat = (
                await conn.execute(
                    select(t.chats.c.telegram_chat_id).where(
                        t.chats.c.id == action["chat_id"]
                    )
                )
            ).scalar_one()
            tguser, author = None, None
            if action["user_id"]:
                author = (
                    (
                        await conn.execute(
                            select(t.users).where(t.users.c.id == action["user_id"])
                        )
                    )
                    .mappings()
                    .one()
                )
                tguser = author["telegram_user_id"]
            incident = (
                (
                    await conn.execute(
                        select(t.incidents).where(
                            t.incidents.c.id == action["incident_id"]
                        )
                    )
                )
                .mappings()
                .first()
                if action["incident_id"]
                else None
            )
        if self.recovery:
            raise RetryLater("recovery_mode", 60)
        if action["state"] in {"in_flight", "unknown"}:
            return await self.reconcile(action, tgchat, tguser)
        policy = Policy.model_validate(settings["policy"])
        deletion_only = (
            action["kind"] == "delete"
            and not action["actor_id"]
            and policy.delete_when_admin_unavailable
            and settings["health_reason"] == "admin_unavailable"
        )
        if (
            not binding
            or binding["version"] != action["binding_version"]
            or settings["lifecycle"] != "connected"
            or settings["version"] != action["policy_version"]
            or incident
            and incident["version"] != action["object_version"]
            or not action["actor_id"]
            and (
                settings["mode"] != "auto"
                or settings["health"] != "healthy"
                and not deletion_only
                or (now() - action["created_at"]).total_seconds()
                > policy.max_action_age_seconds
            )
        ):
            return await self.set_state(aid, "cancelled", "stale_action")
        try:
            if action["actor_id"]:
                await self.auth.scope(
                    action["actor_id"],
                    binding["admin_chat_id"],
                    action["chat_id"],
                    "moderate",
                )
            # Verify both ends without test posts in the working group.
            admin_bot = await self.bot.get_chat_member(
                binding["admin_chat_id"], self.bot.id
            )
            if (
                str(admin_bot.status) not in {"administrator", "creator"}
                and not deletion_only
            ):
                raise Denied("admin_chat_unavailable")
            bot_member = await self.bot.get_chat_member(tgchat, self.bot.id)
            if str(bot_member.status) != "administrator":
                raise Denied("bot_not_admin")
            if action["kind"] == "delete" and not bot_member.can_delete_messages:
                raise Denied("delete_permission_missing")
            if (
                action["kind"] in {"ban", "unban", "mute", "unmute"}
                and not bot_member.can_restrict_members
            ):
                raise Denied("restrict_permission_missing")
            member = await self.bot.get_chat_member(tgchat, tguser) if tguser else None
            if (
                member
                and str(member.status) in {"administrator", "creator"}
                and action["kind"] in {"ban", "mute", "unban", "unmute"}
            ):
                raise Denied("target_is_admin")
            if action["kind"] in {"ban", "unban", "mute", "unmute"} and not member:
                raise Denied("no_human_target")
            before = member.model_dump(mode="json", exclude_none=True) if member else {}
            group_info = (
                await self.bot.get_chat(tgchat) if action["kind"] == "unmute" else None
            )
            if action["kind"] in {"unban", "unmute"}:
                async with self.engine.connect() as conn:
                    if await self.blacklist_blocks_release(conn, action):
                        raise Denied("remove_blacklist_first")
            if action["payload"].get("compensates"):
                async with self.engine.connect() as conn:
                    source = (
                        (
                            await conn.execute(
                                select(t.actions).where(
                                    t.actions.c.id
                                    == uuid.UUID(action["payload"]["compensates"]),
                                    t.actions.c.chat_id == action["chat_id"],
                                    t.actions.c.user_id == action["user_id"],
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    observation = (
                        (
                            await conn.execute(
                                select(t.audit)
                                .where(
                                    t.audit.c.chat_id == action["chat_id"],
                                    t.audit.c.kind == "restriction.observed",
                                    t.audit.c.details["user_id"].as_integer() == tguser,
                                    t.audit.c.created_at >= source["created_at"],
                                )
                                .order_by(t.audit.c.created_at.desc())
                                .limit(1)
                            )
                        )
                        .mappings()
                        .first()
                    )
                    balance = (
                        await conn.execute(
                            select(t.member_states.c.balance).where(
                                t.member_states.c.chat_id == action["chat_id"],
                                t.member_states.c.user_id == action["user_id"],
                            )
                        )
                    ).scalar_one()
                expected = "kicked" if source["kind"] == "ban" else "restricted"
                threshold = policy.ban_at if source["kind"] == "ban" else policy.mute_at
                if (
                    not observation
                    or observation["actor_id"] != self.bot.id
                    or observation["details"].get("status") != expected
                    or str(member.status) != expected
                    or balance >= threshold
                    or source["before"].get("status") != "member"
                ):
                    raise Denied("restriction_origin_unproven")
            if action["kind"] == "unmute":
                async with self.engine.connect() as conn:
                    prior = (
                        (
                            await conn.execute(
                                select(t.actions)
                                .where(
                                    t.actions.c.chat_id == action["chat_id"],
                                    t.actions.c.user_id == action["user_id"],
                                    t.actions.c.kind == "mute",
                                    t.actions.c.state == "succeeded",
                                )
                                .order_by(t.actions.c.created_at.desc())
                                .limit(1)
                            )
                        )
                        .mappings()
                        .first()
                    )
                if not prior or str(member.status) != "restricted":
                    raise Denied("restriction_origin_unknown")
                expected = prior["result"].get("until_date")
                actual = int(member.until_date.timestamp())
                if expected != actual or any(permissions(before).values()):
                    raise Denied("restriction_changed")
                defaults = (
                    permissions(group_info.permissions.model_dump())
                    if group_info.permissions
                    else {}
                )
                previous = permissions(prior["before"])
                # For an ordinary member the preexisting permission set is the group's defaults.
                if prior["before"].get("status") == "member":
                    previous = defaults
                before["restore_permissions"] = {
                    k: defaults.get(k, False) and previous.get(k, False)
                    for k in ChatPermissions.model_fields
                }
        except (Denied, TelegramBadRequest, TelegramForbiddenError) as exc:
            return await self.set_state(
                aid,
                "failed",
                str(exc) if isinstance(exc, Denied) else type(exc).__name__,
            )
        except TelegramRetryAfter as exc:
            raise RetryLater("telegram_rate_limit", exc.retry_after) from exc
        # The group lock linearizes dispatch with pause, rebinding and policy edits.
        async with self.engine.begin() as conn:
            control = await flags(conn, lock=True)
            if control.get("automatic_paused") and not action["actor_id"]:
                await conn.execute(
                    t.actions.update()
                    .where(t.actions.c.id == aid, t.actions.c.state == "planned")
                    .values(state="cancelled", error="system_paused")
                )
                return
            fresh = (
                (
                    await conn.execute(
                        select(t.group_settings)
                        .where(t.group_settings.c.chat_id == action["chat_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            current = (
                (
                    await conn.execute(
                        select(t.actions).where(t.actions.c.id == aid).with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            binding2 = await current_binding(conn, action["chat_id"])
            if (
                current["state"] != "planned"
                or fresh["version"] != action["policy_version"]
                or not binding2
                or binding2["version"] != action["binding_version"]
            ):
                return
            if action["incident_id"]:
                current_version = (
                    await conn.execute(
                        select(t.incidents.c.version).where(
                            t.incidents.c.id == action["incident_id"],
                            t.incidents.c.chat_id == action["chat_id"],
                        )
                    )
                ).scalar_one_or_none()
                if current_version != action["object_version"]:
                    await conn.execute(
                        t.actions.update()
                        .where(t.actions.c.id == aid)
                        .values(state="cancelled", error="revision_changed")
                    )
                    return
            if (
                not action["actor_id"]
                and action["kind"] in {"warn", "mute", "ban"}
                and action["user_id"]
                and not action["payload"].get("deletion_notice")
            ):
                balance = (
                    await conn.execute(
                        select(t.member_states.c.balance).where(
                            t.member_states.c.chat_id == action["chat_id"],
                            t.member_states.c.user_id == action["user_id"],
                        )
                    )
                ).scalar_one()
                minimum = {
                    "warn": policy.warn_at,
                    "mute": policy.mute_at,
                    "ban": policy.ban_at,
                }[action["kind"]]
                if (
                    balance < minimum
                    or action["kind"] == "mute"
                    and policy.ban_enabled
                    and balance >= policy.ban_at
                ):
                    await conn.execute(
                        t.actions.update()
                        .where(t.actions.c.id == aid)
                        .values(state="cancelled", error="balance_changed")
                    )
                    return
            if action["kind"] in {
                "unban",
                "unmute",
            } and await self.blacklist_blocks_release(conn, action):
                await conn.execute(
                    t.actions.update()
                    .where(t.actions.c.id == aid)
                    .values(state="cancelled", error="blacklist_changed")
                )
                return
            await conn.execute(
                t.actions.update()
                .where(t.actions.c.id == aid)
                .values(state="in_flight", before=before, updated_at=now())
            )
        kind, result = action["kind"], {}
        try:
            if kind == "delete":
                await self.bot.delete_message(tgchat, action["payload"]["message_id"])
            elif kind == "ban":
                await self.bot.ban_chat_member(tgchat, tguser, revoke_messages=False)
            elif kind == "unban":
                await self.bot.unban_chat_member(tgchat, tguser, only_if_banned=True)
            elif kind == "mute":
                until = int(
                    (
                        now()
                        + timedelta(
                            seconds=action["payload"].get(
                                "seconds", policy.mute_seconds
                            )
                        )
                    ).timestamp()
                )
                # Persist the exact requested restriction before the request can become uncertain.
                async with self.engine.begin() as conn:
                    await conn.execute(
                        t.actions.update()
                        .where(t.actions.c.id == aid)
                        .values(result={"until_date": until})
                    )
                await self.bot.restrict_chat_member(
                    tgchat,
                    tguser,
                    permissions=ChatPermissions(
                        **{k: False for k in ChatPermissions.model_fields}
                    ),
                    until_date=until,
                    use_independent_chat_permissions=True,
                )
                result["until_date"] = until
            elif kind == "unmute":
                await self.bot.restrict_chat_member(
                    tgchat,
                    tguser,
                    permissions=ChatPermissions(**before["restore_permissions"]),
                    use_independent_chat_permissions=True,
                )
            elif kind == "warn":
                notice, entities = warning_text(action, author)
                sent = await self.bot.send_message(
                    tgchat,
                    notice,
                    parse_mode=None,
                    entities=entities,
                )
                result["message_id"] = sent.message_id
                if policy.notice_ttl_seconds:
                    from app.v2.queue import enqueue

                    async with self.engine.begin() as conn:
                        await enqueue(
                            conn,
                            "notice_cleanup",
                            f"notice:{tgchat}:{sent.message_id}",
                            {"chat_id": tgchat, "message_id": sent.message_id},
                            action["chat_id"],
                            due_at=now() + timedelta(seconds=policy.notice_ttl_seconds),
                        )
            elif kind == "restore":
                content = (
                    "Копия текста восстановлена модератором. Это новая публикация.\n\n"
                    + action["payload"]["text"]
                )
                if len(content.encode("utf-16-le")) > 7200:
                    sent = await self.bot.send_document(
                        tgchat,
                        BufferedInputFile(
                            content.encode("utf-8"), filename="restored-publication.txt"
                        ),
                        caption="Копия сохранённого текста восстановлена модератором.",
                    )
                else:
                    sent = await self.bot.send_message(tgchat, content, parse_mode=None)
                result["message_id"] = sent.message_id
            else:
                raise Conflict("unknown_action_kind")
        except TelegramRetryAfter as exc:
            await self.set_state(aid, "planned", "telegram_rate_limit")
            raise RetryLater("telegram_rate_limit", exc.retry_after) from exc
        except (TelegramBadRequest, TelegramForbiddenError, Conflict) as exc:
            return await self.set_state(aid, "failed", type(exc).__name__)
        except Exception:
            return await self.set_state(aid, "unknown", "telegram_result_unknown")
        await self.set_state(aid, "succeeded", result=result)

    async def reconcile(self, action, tgchat, tguser):
        # Telegram offers no getMessage API: ambiguous deletion/warning is never replayed.
        if action["kind"] not in {"ban", "mute", "unban", "unmute"}:
            return await self.set_state(
                action["id"], "unknown", "manual_reconciliation_required"
            )
        member = await self.bot.get_chat_member(tgchat, tguser)
        status = str(member.status)
        success = action["kind"] == "ban" and status == "kicked"
        success |= action["kind"] == "unban" and status != "kicked"
        if action["kind"] == "mute" and status == "restricted":
            success |= int(member.until_date.timestamp()) == action["result"].get(
                "until_date"
            ) and not any(permissions(member.model_dump()).values())
        # An unmute cannot be attributed solely from an unrestricted snapshot.
        await self.set_state(
            action["id"],
            "succeeded" if success else "unknown",
            None if success else "manual_reconciliation_required",
            result={
                **action["result"],
                "reconciled": success,
                "observed_status": status,
            },
        )
