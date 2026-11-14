"""Transactional member state; no network I/O while holding these locks."""

import uuid

from sqlalchemy import select, func, or_
from sqlalchemy.dialects.postgresql import insert
from app.v2 import tables as t
from app.v2.domain import Policy, Conflict, credit_eligible, now, sanction_for
from app.v2.queue import enqueue, emit


async def ensure_user(conn, telegram_id, **profile):
    permitted = {
        k: v for k, v in profile.items() if k in {"username", "first_name", "last_name"}
    }
    stmt = insert(t.users).values(
        telegram_user_id=telegram_id, is_global_whitelisted=False, **permitted
    )
    if permitted:
        stmt = stmt.on_conflict_do_update(
            index_elements=["telegram_user_id"],
            set_=permitted,
            where=or_(
                *(
                    t.users.c[key].is_distinct_from(value)
                    for key, value in permitted.items()
                )
            ),
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=["telegram_user_id"])
    await conn.execute(stmt)
    return (
        await conn.execute(
            select(t.users.c.id).where(t.users.c.telegram_user_id == telegram_id)
        )
    ).scalar_one()


async def ensure_member(conn, chat_id, user_id):
    await conn.execute(
        insert(t.memberships)
        .values(
            chat_id=chat_id,
            user_id=user_id,
            is_whitelisted=False,
            is_blacklisted=False,
            strike_count=0,
        )
        .on_conflict_do_nothing(index_elements=["chat_id", "user_id"])
    )
    await conn.execute(
        insert(t.member_states)
        .values(chat_id=chat_id, user_id=user_id)
        .on_conflict_do_nothing(index_elements=["chat_id", "user_id"])
    )


async def lock_member(conn, chat_id, user_id):
    await ensure_member(conn, chat_id, user_id)
    return (
        (
            await conn.execute(
                select(t.member_states)
                .where(
                    t.member_states.c.chat_id == chat_id,
                    t.member_states.c.user_id == user_id,
                )
                .with_for_update()
            )
        )
        .mappings()
        .one()
    )


async def active_list(conn, table, chat_id, user_id):
    return (
        (
            await conn.execute(
                select(table).where(
                    table.c.chat_id == chat_id,
                    table.c.user_id == user_id,
                    table.c.revoked_at.is_(None),
                    or_(table.c.expires_at.is_(None), table.c.expires_at > now()),
                )
            )
        )
        .mappings()
        .first()
    )


async def current_binding(conn, chat_id):
    return (
        (
            await conn.execute(
                select(t.bindings).where(
                    t.bindings.c.chat_id == chat_id, t.bindings.c.closed_at.is_(None)
                )
            )
        )
        .mappings()
        .first()
    )


async def audit(conn, chat_id, actor, kind, **details):
    await conn.execute(
        t.audit.insert().values(
            chat_id=chat_id, actor_id=actor, kind=kind, details=details
        )
    )


async def remove_trust(conn, chat_id, user_id, actor_id, reason, *, cutoff=None):
    member = await lock_member(conn, chat_id, user_id)
    changed = await conn.execute(
        t.trust.update()
        .where(
            t.trust.c.chat_id == chat_id,
            t.trust.c.user_id == user_id,
            t.trust.c.revoked_at.is_(None),
        )
        .values(revoked_at=now())
    )
    if changed.rowcount:
        await conn.execute(
            t.member_states.update()
            .where(
                t.member_states.c.chat_id == chat_id,
                t.member_states.c.user_id == user_id,
            )
            .values(
                cycle=member["cycle"] + 1,
                cycle_started_at=cutoff or now(),
                version=member["version"] + 1,
            )
        )
        await audit(
            conn, chat_id, actor_id, "trust.revoked", user_id=user_id, reason=reason
        )
        binding = await current_binding(conn, chat_id)
        if binding:
            notices = (
                (
                    await conn.execute(
                        select(t.outbox).where(
                            t.outbox.c.chat_id == chat_id,
                            t.outbox.c.kind == "trust_notice",
                            t.outbox.c.binding_version == binding["version"],
                            t.outbox.c.state == "sent",
                            t.outbox.c.payload["user_id"].as_integer() == user_id,
                        )
                    )
                )
                .mappings()
                .all()
            )
            for notice in notices:
                if not notice["payload"].get("grant_id"):
                    continue
                grant = (
                    (
                        await conn.execute(
                            select(t.trust).where(
                                t.trust.c.id
                                == uuid.UUID(notice["payload"]["grant_id"]),
                                t.trust.c.cycle == member["cycle"],
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if grant and grant["revoked_at"]:
                    await emit(
                        conn,
                        destination=binding["admin_chat_id"],
                        kind="trust_notice_update",
                        payload={
                            **notice["payload"],
                            "message_id": notice["telegram_message_id"],
                            "revoked_by": actor_id,
                        },
                        key=f"trust-revoked:{grant['id']}",
                        chat_id=chat_id,
                        binding_version=binding["version"],
                    )
    return bool(changed.rowcount)


async def expire_trust(conn, chat_id, user_id):
    row = (
        (
            await conn.execute(
                select(t.trust).where(
                    t.trust.c.chat_id == chat_id,
                    t.trust.c.user_id == user_id,
                    t.trust.c.revoked_at.is_(None),
                    t.trust.c.expires_at <= now(),
                )
            )
        )
        .mappings()
        .first()
    )
    if row:
        await remove_trust(
            conn, chat_id, user_id, None, "Срок доверия истёк", cutoff=row["expires_at"]
        )


async def apply_credit(conn, incident, settings, *, allow_award=True):
    uid, gid = incident["user_id"], incident["chat_id"]
    if uid is None:
        return
    await expire_trust(conn, gid, uid)
    member = await lock_member(conn, gid, uid)
    first = (
        await conn.execute(
            select(func.min(t.items.c.first_received_at)).where(
                t.items.c.chat_id == gid,
                t.items.c.incident_key == incident["incident_key"],
            )
        )
    ).scalar()
    if member["cycle_started_at"] and (
        first is None or first <= member["cycle_started_at"]
    ):
        return
    eligible = credit_eligible(
        incident["model_label"], incident["human_label"], incident["complete"]
    )
    stmt = insert(t.credits).values(
        chat_id=gid,
        user_id=uid,
        cycle=member["cycle"],
        incident_id=incident["id"],
        eligible=eligible,
        source="human" if incident["human_label"] else "model",
    )
    await conn.execute(
        stmt.on_conflict_do_update(
            index_elements=["chat_id", "user_id", "cycle", "incident_id"],
            set_={"eligible": eligible, "source": stmt.excluded.source},
        )
    )
    policy = Policy.model_validate(settings["policy"])
    if not (
        eligible
        and allow_award
        and policy.auto_whitelist_enabled
        and settings["mode"] != "paused"
        and settings["lifecycle"] == "connected"
    ):
        return
    if await active_list(conn, t.deny, gid, uid) or await active_list(
        conn, t.trust, gid, uid
    ):
        return
    count = (
        await conn.execute(
            select(func.count())
            .select_from(t.credits)
            .where(
                t.credits.c.chat_id == gid,
                t.credits.c.user_id == uid,
                t.credits.c.cycle == member["cycle"],
                t.credits.c.eligible.is_(True),
            )
        )
    ).scalar_one()
    if count < policy.auto_whitelist_threshold:
        return
    grant_id = (
        await conn.execute(
            t.trust.insert()
            .values(
                chat_id=gid,
                user_id=uid,
                source="auto",
                reason="Достигнут локальный порог подходящих сообщений",
                cycle=member["cycle"],
                threshold=policy.auto_whitelist_threshold,
            )
            .returning(t.trust.c.id)
        )
    ).scalar_one()
    await audit(
        conn,
        gid,
        None,
        "trust.auto_granted",
        user_id=uid,
        cycle=member["cycle"],
        credits=count,
    )
    binding = await current_binding(conn, gid)
    if binding:
        await emit(
            conn,
            destination=binding["admin_chat_id"],
            kind="trust_notice",
            payload={
                "user_id": uid,
                "credits": count,
                "threshold": policy.auto_whitelist_threshold,
                "grant_id": str(grant_id),
            },
            key=f"trust:{gid}:{uid}:{member['cycle']}",
            chat_id=gid,
            binding_version=binding["version"],
        )


async def plan_action(
    conn, settings, kind, payload, key, *, incident=None, user_id=None, actor=None
):
    binding = await current_binding(conn, settings["chat_id"])
    if not binding:
        raise Conflict("Нет действующей админской связи.")
    stmt = insert(t.actions).values(
        chat_id=settings["chat_id"],
        kind=kind,
        payload=payload,
        key=key,
        user_id=user_id,
        actor_id=actor,
        incident_id=incident["id"] if incident else None,
        object_version=incident["version"] if incident else None,
        policy_version=settings["version"],
        binding_version=binding["version"],
    )
    aid = (
        await conn.execute(
            stmt.on_conflict_do_nothing(index_elements=["key"]).returning(
                t.actions.c.id
            )
        )
    ).scalar()
    if aid:
        await enqueue(
            conn,
            "action",
            "action:" + str(aid),
            {"id": str(aid)},
            settings["chat_id"],
            priority=1,
        )
    return aid


async def contribute_strike(conn, incident, settings, actor=None):
    if incident["user_id"] is None or incident["active_strike"] or incident["legacy"]:
        return
    gid, uid = incident["chat_id"], incident["user_id"]
    member = await lock_member(conn, gid, uid)
    await conn.execute(
        t.strikes.insert().values(
            chat_id=gid,
            user_id=uid,
            incident_id=incident["id"],
            kind="incident",
            delta=1,
            actor_id=actor,
            reason="Подтверждённое нарушение",
        )
    )
    balance = member["balance"] + 1
    await conn.execute(
        t.member_states.update()
        .where(t.member_states.c.chat_id == gid, t.member_states.c.user_id == uid)
        .values(balance=balance, version=member["version"] + 1)
    )
    await conn.execute(
        t.incidents.update()
        .where(t.incidents.c.id == incident["id"])
        .values(active_strike=True)
    )
    incident["active_strike"] = True
    policy = Policy.model_validate(settings["policy"])
    sanction = sanction_for(balance, policy)
    if sanction:
        await plan_action(
            conn,
            settings,
            sanction,
            {"balance": balance, "seconds": policy.mute_seconds},
            f"strike:{incident['id']}:{incident['version']}:{sanction}",
            incident=incident,
            user_id=uid,
            actor=actor,
        )


async def cancel_strike(conn, incident, actor):
    if (
        incident["legacy"]
        or not incident["active_strike"]
        or incident["user_id"] is None
    ):
        return
    member = await lock_member(conn, incident["chat_id"], incident["user_id"])
    changed = await conn.execute(
        t.strikes.update()
        .where(
            t.strikes.c.incident_id == incident["id"],
            t.strikes.c.kind == "incident",
            t.strikes.c.revoked_at.is_(None),
        )
        .values(revoked_at=now())
    )
    if changed.rowcount:
        await conn.execute(
            t.member_states.update()
            .where(
                t.member_states.c.chat_id == incident["chat_id"],
                t.member_states.c.user_id == incident["user_id"],
            )
            .values(
                balance=max(0, member["balance"] - 1), version=member["version"] + 1
            )
        )
    await conn.execute(
        t.incidents.update()
        .where(t.incidents.c.id == incident["id"])
        .values(active_strike=False)
    )
    await conn.execute(
        t.actions.update()
        .where(
            t.actions.c.incident_id == incident["id"], t.actions.c.state == "planned"
        )
        .values(state="cancelled", updated_at=now())
    )
    await audit(
        conn,
        incident["chat_id"],
        actor,
        "strike.cancelled",
        incident_id=str(incident["id"]),
    )
