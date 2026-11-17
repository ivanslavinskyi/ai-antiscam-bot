import hashlib
import json
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.v2 import tables as t
from app.v2.content import extract, normalize
from app.v2.domain import Policy, now
from app.v2.llm import CheckError, RetryLater, PROMPT_VERSION
from app.v2.queue import enqueue, emit
from app.v2.controls import flags
from app.v2.state import (
    ensure_user,
    ensure_member,
    lock_member,
    active_list,
    expire_trust,
    current_binding,
    apply_credit,
    contribute_strike,
    plan_action,
)


async def card(conn, incident, suffix=""):
    binding = await current_binding(conn, incident["chat_id"])
    if binding:
        await emit(
            conn,
            destination=binding["admin_chat_id"],
            kind="incident",
            payload={"id": str(incident["id"])},
            key=f"card:{incident['id']}:{incident['version']}:{suffix}",
            chat_id=incident["chat_id"],
            binding_version=binding["version"],
        )


class Pipeline:
    def __init__(self, engine, bot, classifier, ocr):
        self.engine, self.bot, self.classifier, self.ocr = engine, bot, classifier, ocr

    async def ingest(self, message, update_id):
        if message.from_user and message.from_user.id == self.bot.id:
            return
        content = extract(message)
        if not (content["own"] or content["image_file_id"] or content["unsupported"]):
            return
        async with self.engine.begin() as conn:
            settings = (
                (
                    await conn.execute(
                        select(t.group_settings)
                        .join(
                            t.aliases, t.aliases.c.chat_id == t.group_settings.c.chat_id
                        )
                        .where(t.aliases.c.telegram_chat_id == message.chat.id)
                        .with_for_update(of=t.group_settings)
                    )
                )
                .mappings()
                .first()
            )
            if (
                not settings
                or settings["lifecycle"] != "connected"
                or settings["mode"] == "paused"
            ):
                return
            gid = settings["chat_id"]
            uid = None
            if message.from_user and not message.sender_chat:
                uid = await ensure_user(
                    conn,
                    message.from_user.id,
                    username=message.from_user.username,
                    first_name=message.from_user.first_name,
                    last_name=message.from_user.last_name,
                )
                await ensure_member(conn, gid, uid)
            author = (
                "sender_chat" if message.sender_chat else "user" if uid else "unknown"
            )
            key = (
                "album:" + message.media_group_id
                if message.media_group_id
                else "message:" + str(message.message_id)
            )
            # Album identity includes the publishing principal.
            if message.media_group_id:
                key += ":" + str(message.sender_chat.id if message.sender_chat else uid)
            existing = (
                (
                    await conn.execute(
                        select(t.items)
                        .where(
                            t.items.c.chat_id == gid,
                            t.items.c.telegram_message_id == message.message_id,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            digest = hashlib.sha256(
                json.dumps(content, sort_keys=True).encode()
            ).hexdigest()
            if existing:
                last = (
                    (
                        await conn.execute(
                            select(t.revisions).where(
                                t.revisions.c.item_id == existing["id"],
                                t.revisions.c.revision == existing["revision"],
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                if last["content_hash"] == digest or (
                    last["update_id"] is not None and update_id <= last["update_id"]
                ):
                    return
                iid, revision, key, uid = (
                    existing["id"],
                    existing["revision"] + 1,
                    existing["incident_key"],
                    existing["user_id"],
                )
                await conn.execute(
                    t.items.update()
                    .where(t.items.c.id == iid)
                    .values(revision=revision, edited_at=message.edit_date or now())
                )
            else:
                iid, revision = uuid.uuid4(), 1
                await conn.execute(
                    t.items.insert().values(
                        id=iid,
                        chat_id=gid,
                        telegram_message_id=message.message_id,
                        user_id=uid,
                        sender_chat_id=message.sender_chat.id
                        if message.sender_chat
                        else None,
                        author_kind=author,
                        revision=revision,
                        media_group_id=message.media_group_id,
                        incident_key=key,
                        telegram_date=message.date,
                        edited_at=message.edit_date,
                    )
                )
            rid = uuid.uuid4()
            await conn.execute(
                t.revisions.insert().values(
                    id=rid,
                    chat_id=gid,
                    item_id=iid,
                    revision=revision,
                    update_id=update_id,
                    text=content["own"],
                    normalized=normalize(content["own"]),
                    content=content,
                    content_hash=digest,
                )
            )
            incident = (
                (
                    await conn.execute(
                        select(t.incidents)
                        .where(
                            t.incidents.c.chat_id == gid,
                            t.incidents.c.incident_key == key,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if incident:
                version = incident["version"] + 1
                await conn.execute(
                    t.incidents.update()
                    .where(t.incidents.c.id == incident["id"])
                    .values(
                        version=version,
                        status="pending",
                        model_label=None,
                        confidence=None,
                        human_label=None,
                        updated_at=now(),
                    )
                )
                inc = dict(incident, version=version)
                # Already executed actions stay in history; queued stale actions are invalidated.
                await conn.execute(
                    t.actions.update()
                    .where(
                        t.actions.c.incident_id == incident["id"],
                        t.actions.c.state == "planned",
                    )
                    .values(state="cancelled", error="revision_changed")
                )
                await conn.execute(
                    t.credits.update()
                    .where(t.credits.c.incident_id == incident["id"])
                    .values(eligible=False)
                )
            else:
                inc = dict(
                    id=uuid.uuid4(),
                    chat_id=gid,
                    incident_key=key,
                    user_id=uid,
                    version=1,
                    created_at=now(),
                )
                await conn.execute(t.incidents.insert().values(**inc))
            due = now()
            if message.media_group_id:
                due = min(
                    now() + timedelta(seconds=2),
                    inc["created_at"] + timedelta(seconds=10),
                )
            await enqueue(
                conn,
                "check",
                f"check:{inc['id']}:{inc['version']}",
                {"id": str(inc["id"]), "version": inc["version"]},
                gid,
                due_at=due,
            )

    async def check(self, job):
        iid = uuid.UUID(job["payload"]["id"])
        version = job["payload"]["version"]
        async with self.engine.begin() as conn:
            inc = (
                (await conn.execute(select(t.incidents).where(t.incidents.c.id == iid)))
                .mappings()
                .one()
            )
            if inc["version"] != version or inc["status"] != "pending":
                return
            settings = (
                (
                    await conn.execute(
                        select(t.group_settings).where(
                            t.group_settings.c.chat_id == inc["chat_id"]
                        )
                    )
                )
                .mappings()
                .one()
            )
            if settings["lifecycle"] != "connected" or settings["mode"] == "paused":
                return
            policy = Policy.model_validate(settings["policy"])
            rows = [
                dict(r)
                for r in (
                    await conn.execute(
                        select(
                            t.revisions,
                            t.items.c.telegram_message_id,
                            t.items.c.telegram_date,
                        )
                        .join(
                            t.items,
                            (t.items.c.id == t.revisions.c.item_id)
                            & (t.items.c.revision == t.revisions.c.revision),
                        )
                        .where(
                            t.items.c.chat_id == inc["chat_id"],
                            t.items.c.incident_key == inc["incident_key"],
                        )
                        .order_by(t.items.c.telegram_message_id)
                    )
                ).mappings()
            ]
            skip = None
            telegram_user = None
            denied = False
            if inc["user_id"]:
                await expire_trust(conn, inc["chat_id"], inc["user_id"])
                telegram_user = (
                    await conn.execute(
                        select(t.users.c.telegram_user_id).where(
                            t.users.c.id == inc["user_id"]
                        )
                    )
                ).scalar_one()
                denied = bool(
                    await active_list(conn, t.deny, inc["chat_id"], inc["user_id"])
                )
                if not denied and await active_list(
                    conn, t.trust, inc["chat_id"], inc["user_id"]
                ):
                    skip = "trusted"
            tgchat = (
                await conn.execute(
                    select(t.chats.c.telegram_chat_id).where(
                        t.chats.c.id == inc["chat_id"]
                    )
                )
            ).scalar_one()
        # A current local trust grant already exempts this publication. Do not
        # let an unnecessary Telegram membership request turn it into an error.
        # Explicit local deny still takes precedence when resolving lists above.
        if skip == "trusted":
            return await self.finish(inc, version, settings, rows, skip=skip)
        if policy.exempt_admins and telegram_user:
            try:
                member = await self.bot.get_chat_member(tgchat, telegram_user)
                if str(member.status) in {"administrator", "creator"}:
                    skip = "administrator"
            except Exception:
                # Cannot establish exemption or sanction eligibility safely.
                return await self.finish(
                    inc, version, settings, rows, error="membership_unavailable"
                )
        if skip:
            return await self.finish(inc, version, settings, rows, skip=skip)
        if denied:
            # Blacklist is explicit local deny, separately recorded from model classification.
            return await self.finish(inc, version, settings, rows, skip="blacklisted")
        own, quoted, links, contexts = [], [], [], []
        try:
            for row in rows:
                content = row["content"]
                context = content.get("reply_context")
                if context and context.get("incomplete"):
                    context = await self.restore_local_context(inc, tgchat, context)
                    content = {**content, "reply_context": context}
                    async with self.engine.begin() as conn:
                        await conn.execute(
                            t.revisions.update()
                            .where(t.revisions.c.id == row["id"])
                            .values(content=content)
                        )
                    if context["incomplete"]:
                        raise CheckError("reply_context_incomplete")
                if content.get("unsupported"):
                    raise CheckError("unsupported_media")
                ocr = row["ocr_text"]
                if content.get("image_file_id") and not ocr:
                    ocr = await self.ocr.extract(
                        content["image_file_id"], content.get("image_size")
                    )
                    async with self.engine.begin() as conn:
                        await conn.execute(
                            t.revisions.update()
                            .where(t.revisions.c.id == row["id"])
                            .values(ocr_text=ocr)
                        )
                own.append(normalize(row["text"] + "\n" + ocr))
                quoted.append(content.get("quoted_context", "") if not context else "")
                if context or content.get("forwarded"):
                    contexts.append(
                        {
                            "publication_index": len(own) - 1,
                            "forwarded": content.get("forwarded", False),
                            "forward_source": content.get("forward_source"),
                            "reply": context,
                        }
                    )
                links.extend(content.get("links", []))
            if not any(s.strip() for s in own) and not links:
                raise CheckError("no_readable_content")
            verdict, meta = await self.classifier.classify(
                inc["chat_id"],
                f"llm:{job['id']}:{job['attempt']}",
                {
                    "own_publication": own,
                    "links": links,
                    "quoted_context": quoted,
                    "publication_context": contexts,
                    "community_context": policy.context,
                },
                policy.daily_budget_usd,
            )
        except RetryLater:
            raise
        except CheckError as exc:
            if (
                str(exc) == "provider_transport"
                or str(exc).startswith("provider_http_5")
            ) and job["attempt"] < 3:
                raise RetryLater(str(exc), 5 * job["attempt"]) from exc
            return await self.finish(inc, version, settings, rows, error=str(exc))
        except Exception:
            return await self.finish(
                inc, version, settings, rows, error="media_or_provider_error"
            )
        await self.finish(inc, version, settings, rows, verdict=verdict, meta=meta)

    async def restore_local_context(self, inc, tgchat, context):
        """Use only an unedited, already known source in this same work group."""
        source = context.get("source", {})
        if (
            context.get("truncated")
            or source.get("chat_id") != tgchat
            or not source.get("message_id")
        ):
            return context
        async with self.engine.connect() as conn:
            saved = (
                (
                    await conn.execute(
                        select(
                            t.revisions.c.text,
                            t.revisions.c.ocr_text,
                            t.revisions.c.content,
                        )
                        .join(
                            t.items,
                            (t.items.c.id == t.revisions.c.item_id)
                            & (t.items.c.revision == t.revisions.c.revision),
                        )
                        .where(
                            t.items.c.chat_id == inc["chat_id"],
                            t.items.c.telegram_message_id == source["message_id"],
                            t.items.c.incident_key != inc["incident_key"],
                            t.items.c.revision == 1,
                            t.items.c.first_received_at <= inc["created_at"],
                        )
                    )
                )
                .mappings()
                .first()
            )
        if not saved:
            return context
        full = (saved["text"] + "\n" + saved["ocr_text"]).strip()
        quote = context.get("quote", "")
        links = saved["content"].get("links", [])
        unread = saved["content"].get("unsupported") or (
            saved["content"].get("image_file_id") and not saved["ocr_text"]
        )
        if (
            not full
            or len(full) > 2000
            or unread
            or (quote and quote not in full)
            or len(links) > 10
            or any(len(x) > 1000 for x in links)
        ):
            return context
        return {
            **context,
            "text": full,
            "links": links,
            "coverage": "stored_local",
            "incomplete": False,
            "media_unread": False,
        }

    async def finish(
        self,
        inc,
        version,
        settings,
        rows,
        *,
        verdict=None,
        meta=None,
        error=None,
        skip=None,
    ):
        async with self.engine.begin() as conn:
            # Lock ordering is group -> member -> incident throughout the write services.
            current_settings = (
                (
                    await conn.execute(
                        select(t.group_settings)
                        .where(t.group_settings.c.chat_id == inc["chat_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if inc["user_id"]:
                await lock_member(conn, inc["chat_id"], inc["user_id"])
            current = (
                (
                    await conn.execute(
                        select(t.incidents)
                        .where(t.incidents.c.id == inc["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if current["version"] != version or current["status"] != "pending":
                return
            stale = (
                current_settings["version"] != settings["version"]
                or current_settings["mode"] == "paused"
                or current_settings["lifecycle"] != "connected"
            )
            policy = Policy.model_validate(current_settings["policy"])
            aged = any(
                (now() - r["telegram_date"]).total_seconds()
                > policy.max_action_age_seconds
                for r in rows
            )
            status = (
                "skipped"
                if skip
                else "error"
                if error
                else "review"
                if stale or aged
                else "checked"
            )
            values = dict(
                status=status, complete=not bool(error or skip), updated_at=now()
            )
            if verdict:
                if verdict.label == "REVIEW":
                    values.update(status="review", complete=False)
                values.update(
                    model_label=verdict.label,
                    category=verdict.category,
                    confidence=verdict.confidence,
                    reason=verdict.reason,
                )
                if verdict.label == "SCAM" and (
                    verdict.confidence < policy.confidence_threshold
                    or verdict.category not in policy.categories
                ):
                    values["status"] = "review"
            else:
                values["reason"] = error or skip
            await conn.execute(
                t.incidents.update()
                .where(t.incidents.c.id == inc["id"])
                .values(**values)
            )
            updated = dict(current, **values)
            # One check per incident revision, album rows link through its publication items.
            await conn.execute(
                t.checks.insert().values(
                    chat_id=inc["chat_id"],
                    revision_id=rows[-1]["id"],
                    incident_id=inc["id"],
                    model_requested=self.classifier.settings.openai_model,
                    prompt_version=PROMPT_VERSION,
                    policy_version=settings["version"],
                    status="error" if error else "skipped" if skip else "ok",
                    source="live",
                    label=verdict.label if verdict else None,
                    category=verdict.category if verdict else None,
                    confidence=verdict.confidence if verdict else None,
                    reason=error or skip or verdict.reason,
                    error=error,
                    **(meta or {}),
                )
            )
            if verdict and not stale:
                await apply_credit(conn, updated, current_settings)
            automatic = (
                not stale
                and not aged
                and not current["legacy"]
                and current_settings["mode"] == "auto"
                and current_settings["health"] == "healthy"
                and values["status"] == "checked"
            )
            system_paused = (await flags(conn)).get("automatic_paused", False)
            automatic = automatic and not system_paused
            deletion_only = (
                not stale
                and not aged
                and not current["legacy"]
                and current_settings["mode"] == "auto"
                and values["status"] == "checked"
                and current_settings["health_reason"] == "admin_unavailable"
                and policy.delete_when_admin_unavailable
                and not system_paused
            )
            if (automatic or deletion_only) and verdict and verdict.label == "SCAM":
                for row in rows:
                    await plan_action(
                        conn,
                        current_settings,
                        "delete",
                        {"message_id": row["telegram_message_id"]},
                        f"delete:{row['id']}",
                        incident=updated,
                        user_id=inc["user_id"],
                    )
                if automatic:
                    await contribute_strike(conn, updated, current_settings)
            if (
                skip == "blacklisted"
                and not stale
                and not aged
                and current_settings["mode"] == "auto"
                and current_settings["health"] == "healthy"
                and not system_paused
            ):
                for row in rows:
                    await plan_action(
                        conn,
                        current_settings,
                        "delete",
                        {"message_id": row["telegram_message_id"]},
                        f"denydelete:{row['id']}",
                        incident=updated,
                        user_id=inc["user_id"],
                    )
            if (
                error
                or verdict
                and (verdict.label == "SCAM" or values["status"] == "review")
            ) and policy.review_enabled:
                await card(conn, updated, "result")
