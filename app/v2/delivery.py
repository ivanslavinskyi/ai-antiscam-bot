import uuid
from types import SimpleNamespace

from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from sqlalchemy import select

from app.v2 import tables as t
from app.v2.llm import RetryLater
from app.v2.domain import Policy, quiet_delay
from app.v2.state import current_binding
from app.v2.auth import owner
from app.v2.ui import clip, incident_text, incident_buttons, keyboard
from app.v2.card_view import code_entities, trust_text


class Delivery:
    def __init__(self, engine, bot, onboarding):
        self.engine, self.bot, self.onboarding = engine, bot, onboarding

    async def execute(self, job):
        oid = uuid.UUID(job["payload"]["id"])
        async with self.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        select(t.outbox).where(t.outbox.c.id == oid).with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if row["state"] in {"sent", "cancelled", "failed", "unknown"}:
                return
            if (
                row["kind"] == "budget"
                or (
                    row["kind"] in {"application", "group_request"}
                    and row["payload"].get("owner_only")
                )
            ) and not await owner(conn, row["destination"]):
                await conn.execute(
                    t.outbox.update()
                    .where(t.outbox.c.id == oid)
                    .values(
                        state="unknown" if row["state"] == "in_flight" else "cancelled"
                    )
                )
                return
            if row["state"] == "in_flight":
                await conn.execute(
                    t.outbox.update()
                    .where(t.outbox.c.id == oid)
                    .values(state="unknown")
                )
                return
            if row["chat_id"]:
                binding = await current_binding(conn, row["chat_id"])
                if (
                    not binding
                    or binding["version"] != row["binding_version"]
                    or (
                        binding["admin_chat_id"] != row["destination"]
                        and row["kind"] != "onboarding_transfer"
                    )
                ):
                    await conn.execute(
                        t.outbox.update()
                        .where(t.outbox.c.id == oid)
                        .values(state="cancelled")
                    )
                    return
                if row["kind"] == "summary":
                    policy = (
                        await conn.execute(
                            select(t.group_settings.c.policy).where(
                                t.group_settings.c.chat_id == row["chat_id"]
                            )
                        )
                    ).scalar_one()
                    delay = quiet_delay(Policy.model_validate(policy))
                    if delay:
                        raise RetryLater("quiet_hours", delay)
            buttons, inc, previous = None, None, []
            entities = []
            p = row["payload"]
            if row["kind"] == "incident":
                inc = (
                    (
                        await conn.execute(
                            select(
                                t.incidents,
                                t.users.c.telegram_user_id,
                                t.users.c.username,
                                t.users.c.first_name,
                                t.users.c.last_name,
                                t.group_settings.c.policy["timezone"]
                                .as_string()
                                .label("timezone"),
                            )
                            .outerjoin(t.users, t.users.c.id == t.incidents.c.user_id)
                            .join(
                                t.group_settings,
                                t.group_settings.c.chat_id == t.incidents.c.chat_id,
                            )
                            .where(
                                t.incidents.c.id == uuid.UUID(p["id"]),
                                t.incidents.c.chat_id == row["chat_id"],
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                title = (
                    await conn.execute(
                        select(t.chats.c.title).where(t.chats.c.id == row["chat_id"])
                    )
                ).scalar_one()
                texts = (
                    (
                        await conn.execute(
                            select(t.revisions.c.text, t.revisions.c.ocr_text)
                            .join(
                                t.items,
                                (t.items.c.id == t.revisions.c.item_id)
                                & (t.items.c.revision == t.revisions.c.revision),
                            )
                            .where(
                                t.items.c.chat_id == row["chat_id"],
                                t.items.c.incident_key == inc["incident_key"],
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                actions = (
                    (
                        await conn.execute(
                            select(
                                t.actions.c.kind, t.actions.c.state, t.actions.c.error
                            )
                            .where(
                                t.actions.c.chat_id == row["chat_id"],
                                t.actions.c.incident_id == inc["id"],
                            )
                            .order_by(t.actions.c.created_at, t.actions.c.id)
                        )
                    )
                    .mappings()
                    .all()
                )
                from app.v2.card_state import member_card_state

                card_row = dict(
                    inc,
                    texts=texts,
                    actions=actions,
                    **await member_card_state(conn, inc),
                )
                content = incident_text(card_row, title)
                buttons = incident_buttons(row["chat_id"], card_row)
                entities = code_entities(content, inc.get("telegram_user_id"))
                previous = (
                    (
                        await conn.execute(
                            select(t.cards).where(
                                t.cards.c.chat_id == row["chat_id"],
                                t.cards.c.incident_id == inc["id"],
                                t.cards.c.admin_chat_id == row["destination"],
                                t.cards.c.binding_version == row["binding_version"],
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            elif row["kind"] in {"application", "group_request"}:
                content = p["text"]
                command = "app" if row["kind"] == "application" else "grouprequest"
                buttons = keyboard(
                    [
                        [
                            (
                                "Открыть заявку",
                                f"v:{command}:{uuid.UUID(p['request']).hex}",
                            ),
                            ("Меню", "v:system:0"),
                        ]
                    ]
                )
            elif row["kind"] in {"onboarding", "onboarding_transfer"}:
                content = (
                    f"Группа «{p['title']}» подключается к этой админской группе.\n"
                )
                content += "После доставки этой карточки включится наблюдение. Откройте /as для настройки и перехода к автоматической модерации."
                buttons = keyboard(
                    [
                        [
                            (
                                "Открыть панель",
                                f"v:onopen:{uuid.UUID(p['request']).hex}:{p['receipt']}",
                            )
                        ]
                    ]
                )
            elif row["kind"] in {"trust_notice", "trust_notice_update"}:
                grant_query = (
                    select(
                        t.trust,
                        t.users.c.telegram_user_id,
                        t.users.c.first_name,
                        t.users.c.last_name,
                        t.users.c.username,
                    )
                    .join(t.users, t.users.c.id == t.trust.c.user_id)
                    .where(
                        t.trust.c.chat_id == row["chat_id"],
                        t.trust.c.user_id == p["user_id"],
                        t.trust.c.source == "auto",
                    )
                )
                if p.get("grant_id"):
                    grant_query = grant_query.where(
                        t.trust.c.id == uuid.UUID(p["grant_id"])
                    )
                else:
                    # Historical queued notices: cycle is part of their durable key.
                    grant_query = grant_query.where(
                        t.trust.c.cycle == int(row["key"].rsplit(":", 1)[-1])
                    )
                grant = (await conn.execute(grant_query)).mappings().first()
                if not grant:
                    await conn.execute(
                        t.outbox.update()
                        .where(t.outbox.c.id == oid)
                        .values(state="cancelled")
                    )
                    return
                title = (
                    await conn.execute(
                        select(t.chats.c.title).where(t.chats.c.id == row["chat_id"])
                    )
                ).scalar_one()
                content = trust_text(grant, title, p["credits"], p["threshold"])
                entities = code_entities(content, grant["telegram_user_id"])
                gid = row["chat_id"]
                rows = (
                    []
                    if grant["revoked_at"]
                    else [
                        [("Убрать из вайтлиста", f"v:tgremove:{gid}:{grant['id'].hex}")]
                    ]
                )
                rows += [
                    [("Участник", f"v:user:{gid}:{grant['user_id']}")],
                    [
                        ("Настройки автовайтлиста", f"v:settings:{gid}:0"),
                        ("Меню", f"v:menu:{gid}"),
                    ],
                ]
                buttons = keyboard(rows)
                if row["kind"] == "trust_notice_update":
                    previous = [{"message_id": p["message_id"]}]
                    if p.get("revoked_by"):
                        content += f"\n🧑‍💼 Снял доверие: {p['revoked_by']}"
            else:
                content = p.get("text", "Уведомление группы " + str(row["chat_id"]))
            await conn.execute(
                t.outbox.update().where(t.outbox.c.id == oid).values(state="in_flight")
            )
        try:
            updated = []
            for old in previous:
                try:
                    await self.bot.edit_message_text(
                        clip(content),
                        chat_id=row["destination"],
                        message_id=old["message_id"],
                        reply_markup=buttons,
                        parse_mode=None,
                        entities=entities,
                    )
                    updated.append(old)
                except TelegramBadRequest as exc:
                    if "message is not modified" in exc.message.lower():
                        updated.append(old)
                    # An unavailable old card must not prevent delivery of the current result.
            if row["kind"] == "trust_notice_update" and not updated:
                async with self.engine.begin() as conn:
                    await conn.execute(
                        t.outbox.update()
                        .where(t.outbox.c.id == oid)
                        .values(state="failed")
                    )
                return
            if updated:
                sent = SimpleNamespace(message_id=updated[-1]["message_id"])
            else:
                sent = await self.bot.send_message(
                    row["destination"],
                    clip(content),
                    reply_markup=buttons,
                    parse_mode=None,
                    entities=entities,
                )
        except TelegramRetryAfter as exc:
            async with self.engine.begin() as conn:
                await conn.execute(
                    t.outbox.update()
                    .where(t.outbox.c.id == oid, t.outbox.c.state == "in_flight")
                    .values(state="pending")
                )
            raise RetryLater("telegram_rate_limit", exc.retry_after) from exc
        except (TelegramBadRequest, TelegramForbiddenError):
            async with self.engine.begin() as conn:
                await conn.execute(
                    t.outbox.update()
                    .where(t.outbox.c.id == oid, t.outbox.c.state == "in_flight")
                    .values(state="failed")
                )
            return
        except Exception:
            async with self.engine.begin() as conn:
                await conn.execute(
                    t.outbox.update()
                    .where(t.outbox.c.id == oid, t.outbox.c.state == "in_flight")
                    .values(state="unknown")
                )
            return
        async with self.engine.begin() as conn:
            await conn.execute(
                t.outbox.update()
                .where(t.outbox.c.id == oid)
                .values(state="sent", telegram_message_id=sent.message_id)
            )
            if row["kind"] in {"onboarding", "onboarding_transfer"}:
                await self.onboarding.activate(conn, row)
            elif inc and updated:
                await conn.execute(
                    t.cards.update()
                    .where(t.cards.c.id.in_([r["id"] for r in updated]))
                    .values(version=inc["version"])
                )
            elif inc:
                await conn.execute(
                    t.cards.insert().values(
                        chat_id=row["chat_id"],
                        incident_id=inc["id"],
                        admin_chat_id=row["destination"],
                        message_id=sent.message_id,
                        binding_version=row["binding_version"],
                        version=inc["version"],
                    )
                )
