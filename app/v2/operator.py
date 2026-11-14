"""All reads/writes are explicitly scoped, including exports and old callbacks."""

import csv
import io
import secrets
import uuid
from datetime import timedelta, datetime, timezone

from sqlalchemy import select, func, tuple_, or_

from app.v2 import tables as t
from app.v2.auth import assert_scope, owner
from app.v2.domain import Conflict, Denied, Policy, now
from app.v2.pipeline import card
from app.v2.queue import enqueue
from app.v2.state import (
    lock_member,
    ensure_user,
    active_list,
    remove_trust,
    expire_trust,
    audit,
    apply_credit,
    contribute_strike,
    cancel_strike,
    plan_action,
)

PERMISSION = {
    "delete": "moderate",
    "label": "moderate",
    "ban": "moderate",
    "unban": "moderate",
    "mute": "moderate",
    "unmute": "moderate",
    "reset_strikes": "moderate",
    "retry_check": "moderate",
    "retry_job": "settings",
    "retry_action": "moderate",
    "restore": "moderate",
    "trust_add": "trust",
    "trust_remove": "trust",
    "deny_add": "moderate",
    "deny_remove": "moderate",
    "settings": "settings",
    "mode": "settings",
    "role": "roles",
    "import_trust": "settings",
    "disconnect": "system",
}


def csv_safe(value):
    value = "" if value is None else str(value)
    if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        value = "'" + value
    return value


def parse_import(data):
    if len(data) > 2 * 1024 * 1024:
        raise Conflict("Файл превышает 2 МБ.")
    try:
        rows = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        result = {}
        for number, row in enumerate(rows, start=2):
            if number > 10001:
                raise Conflict("В файле больше 10 000 записей.")
            uid = int(row["telegram_user_id"].strip().removeprefix("'"))
            if not 0 < uid <= 9223372036854775807:
                raise ValueError()
            reason = (row.get("reason") or "Импорт")[:500]
            expires = (row.get("expires_at") or "").strip()
            if expires:
                date = datetime.fromisoformat(expires)
                if date.tzinfo is None or date <= now():
                    raise Conflict(
                        f"Строка {number}: expires_at должен быть будущей ISO-датой с часовым поясом."
                    )
                expires = date.astimezone(timezone.utc).isoformat()
            if uid in result and result[uid]["expires_at"] != (expires or None):
                raise Conflict(
                    f"Строка {number}: у повторяющегося ID разные сроки доверия."
                )
            result[uid] = {
                "telegram_user_id": uid,
                "reason": reason,
                "expires_at": expires or None,
            }
        if not result:
            raise ValueError()
        return list(result.values())
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        UnicodeError,
        csv.Error,
    ) as exc:
        raise Conflict(
            "Нужен UTF-8 CSV: telegram_user_id, необязательные reason/expires_at. До 10 000 строк; даты ISO с часовым поясом."
        ) from exc


class Operator:
    def __init__(self, engine, auth):
        self.engine, self.auth = engine, auth

    async def issue(self, scope, kind, payload, object_version=None):
        if kind == "settings" and "daily_budget_usd" in payload.get("changes", {}):
            raise Denied("Бюджеты меняет владелец бота в личной панели «Бюджеты».")
        permission = PERMISSION.get(kind)
        if not permission:
            raise Conflict("Неизвестное действие.")
        async with self.engine.begin() as conn:
            await assert_scope(conn, scope, permission)
            settings = (
                (
                    await conn.execute(
                        select(t.group_settings).where(
                            t.group_settings.c.chat_id == scope.chat_id
                        )
                    )
                )
                .mappings()
                .one()
            )
            if (
                payload.get("expected_policy_version", settings["version"])
                != settings["version"]
            ):
                raise Conflict("Настройки изменились. Откройте актуальную панель.")
            token = secrets.token_urlsafe(18)
            await conn.execute(
                t.operations.insert().values(
                    token=token,
                    actor_id=scope.actor_id,
                    admin_chat_id=scope.admin_chat_id,
                    chat_id=scope.chat_id,
                    binding_version=scope.binding_version,
                    object_version=object_version,
                    kind=kind,
                    payload={**payload, "policy_version": settings["version"]},
                    expires_at=now() + timedelta(minutes=5),
                )
            )
        return token

    async def confirm(self, actor, admin, token):
        async with self.engine.connect() as conn:
            op = (
                (
                    await conn.execute(
                        select(t.operations).where(
                            t.operations.c.token == token,
                            t.operations.c.actor_id == actor,
                            t.operations.c.admin_chat_id == admin,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if not op:
            raise Denied(
                "Подтверждение принадлежит другому пользователю или недействительно."
            )
        if op["kind"] == "settings" and "daily_budget_usd" in op["payload"].get(
            "changes", {}
        ):
            raise Denied(
                "Старое подтверждение бюджета отключено. Владелец меняет бюджет в личной панели."
            )
        scope = await self.auth.scope(
            actor, admin, op["chat_id"], PERMISSION[op["kind"]]
        )
        if op["kind"] == "mode" and op["payload"].get("mode") == "auto":
            async with self.engine.connect() as conn:
                work = (
                    await conn.execute(
                        select(t.chats.c.telegram_chat_id).where(
                            t.chats.c.id == scope.chat_id
                        )
                    )
                ).scalar_one()
            for chat in (work, admin):
                try:
                    bot_member = await self.auth.bot.get_chat_member(
                        chat, self.auth.bot.id
                    )
                except Exception as exc:
                    raise Conflict(
                        "Не удалось проверить права бота. Боевой режим не включён."
                    ) from exc
                if str(bot_member.status) not in {"administrator", "creator"}:
                    raise Conflict("Боту нужны права администратора в обеих группах.")
                if (
                    chat == work
                    and str(bot_member.status) != "creator"
                    and not (
                        getattr(bot_member, "can_delete_messages", False)
                        and getattr(bot_member, "can_restrict_members", False)
                    )
                ):
                    raise Conflict(
                        "Не хватает прав удаления или ограничения участников."
                    )
        if op["kind"] == "role" and op["payload"]["role"] != "remove":
            target = await self.auth.bot.get_chat_member(
                admin, int(op["payload"]["actor_id"])
            )
            if str(target.status) not in {"creator", "administrator", "member"}:
                raise Denied("Получатель роли должен состоять в админской группе.")
        async with self.engine.begin() as conn:
            settings = dict(
                (
                    await conn.execute(
                        select(t.group_settings)
                        .where(t.group_settings.c.chat_id == scope.chat_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            await assert_scope(conn, scope, PERMISSION[op["kind"]])
            op = (
                (
                    await conn.execute(
                        select(t.operations)
                        .where(t.operations.c.token == token)
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if op["used_at"]:
                return op["result"]
            if (
                op["expires_at"] <= now()
                or op["binding_version"] != scope.binding_version
            ):
                raise Conflict("Подтверждение устарело. Откройте актуальную карточку.")
            if settings["version"] != op["payload"]["policy_version"]:
                raise Conflict("Настройки изменились. Проверьте действие заново.")
            if settings["lifecycle"] != "connected":
                raise Conflict("Группа отключена; доступна история.")
            before = {
                "policy": settings["policy"],
                "mode": settings["mode"],
                "version": settings["version"],
            }
            if op["payload"].get("user_id"):
                member_before = (
                    (
                        await conn.execute(
                            select(t.member_states).where(
                                t.member_states.c.chat_id == scope.chat_id,
                                t.member_states.c.user_id
                                == int(op["payload"]["user_id"]),
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if member_before:
                    before["member"] = {
                        k: member_before[k]
                        for k in ["balance", "legacy_balance", "cycle", "version"]
                    }
            result = await self.apply(conn, scope, settings, op)
            await conn.execute(
                t.operations.update()
                .where(t.operations.c.token == token)
                .values(used_at=now(), result=result)
            )
            await audit(
                conn,
                scope.chat_id,
                actor,
                "operator." + op["kind"],
                token=token,
                result=result,
                before=before,
                requested={k: v for k, v in op["payload"].items() if k != "rows"},
            )
            return result

    async def cancel(self, actor, admin, token):
        async with self.engine.begin() as conn:
            op = (
                (
                    await conn.execute(
                        select(t.operations)
                        .where(
                            t.operations.c.token == token,
                            t.operations.c.actor_id == actor,
                            t.operations.c.admin_chat_id == admin,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if not op:
                raise Denied("Подтверждение принадлежит другому пользователю.")
            if op["used_at"]:
                return op["result"]
            result = {"message": "Действие отменено."}
            await conn.execute(
                t.operations.update()
                .where(t.operations.c.token == token)
                .values(used_at=now(), result=result)
            )
            await audit(conn, op["chat_id"], actor, "operator.cancel", token=token)
            return result

    async def apply(self, conn, scope, settings, op):
        p, kind, gid = op["payload"], op["kind"], scope.chat_id
        if kind in {"settings", "mode", "disconnect"}:
            values = {"version": settings["version"] + 1, "updated_at": now()}
            if kind == "settings":
                if "daily_budget_usd" in p["changes"]:
                    raise Denied("Бюджеты меняет только владелец в личной панели.")
                values["policy"] = Policy.model_validate(
                    {**settings["policy"], **p["changes"]}
                ).model_dump()
            elif kind == "mode":
                if p["mode"] not in {"paused", "auto", "observe"}:
                    raise Conflict("Неизвестный режим.")
                if p["mode"] == "auto" and settings["health"] != "healthy":
                    raise Conflict(
                        "Сначала восстановите права и доступность админки в разделе состояния."
                    )
                values["mode"] = p["mode"]
            else:
                values.update(lifecycle="disconnected", mode="paused")
            await conn.execute(
                t.group_settings.update()
                .where(t.group_settings.c.chat_id == gid)
                .values(**values)
            )
            await conn.execute(
                t.actions.update()
                .where(t.actions.c.chat_id == gid, t.actions.c.state == "planned")
                .values(state="cancelled", error="policy_changed", updated_at=now())
            )
            inflight = (
                await conn.execute(
                    select(func.count())
                    .select_from(t.actions)
                    .where(
                        t.actions.c.chat_id == gid,
                        t.actions.c.state.in_(["in_flight", "unknown"]),
                    )
                )
            ).scalar_one()
            return {
                "message": "Настройки сохранены. Уже выданное доверие сохранено.",
                "in_flight_or_unknown": inflight,
            }
        if kind == "role":
            target, role = int(p["actor_id"]), p["role"]
            if target <= 0 or role not in {
                "manager",
                "moderator",
                "observer",
                "remove",
            }:
                raise Conflict("Недопустимая локальная роль.")
            existing = (
                (
                    await conn.execute(
                        select(t.roles).where(
                            t.roles.c.chat_id == gid,
                            t.roles.c.actor_id == target,
                            t.roles.c.revoked_at.is_(None),
                        )
                    )
                )
                .mappings()
                .first()
            )
            managers = (
                await conn.execute(
                    select(func.count())
                    .select_from(t.roles)
                    .where(
                        t.roles.c.chat_id == gid,
                        t.roles.c.role == "manager",
                        t.roles.c.revoked_at.is_(None),
                    )
                )
            ).scalar_one()
            if "expected_role_id" in p and p["expected_role_id"] != (
                str(existing["id"]) if existing else None
            ):
                raise Conflict("Роль уже изменилась. Откройте команду заново.")
            if (
                role == "manager" or existing and existing["role"] == "manager"
            ) and not await owner(conn, scope.actor_id):
                raise Denied(
                    "Назначить или заменить управляющего может только владелец системы."
                )
            if (
                existing
                and existing["role"] == "manager"
                and role != "manager"
                and managers <= 1
                and not await owner(conn, scope.actor_id)
            ):
                raise Denied("Последнего менеджера может заменить владелец системы.")
            if p.get("replace_managers"):
                if role != "manager" or not await owner(conn, scope.actor_id):
                    raise Denied("Передачу управления подтверждает владелец.")
                old_managers = (
                    (
                        await conn.execute(
                            select(t.roles).where(
                                t.roles.c.chat_id == gid,
                                t.roles.c.role == "manager",
                                t.roles.c.revoked_at.is_(None),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                if sorted(str(r["id"]) for r in old_managers) != p.get(
                    "expected_managers"
                ):
                    raise Conflict(
                        "Управляющий уже изменён. Откройте актуальную панель."
                    )
                for previous in old_managers:
                    if previous["actor_id"] == target:
                        continue
                    await conn.execute(
                        t.roles.update()
                        .where(t.roles.c.id == previous["id"])
                        .values(revoked_at=now())
                    )
                    if p.get("keep_previous"):
                        await conn.execute(
                            t.roles.insert().values(
                                chat_id=gid,
                                actor_id=previous["actor_id"],
                                role="moderator",
                                granted_by=scope.actor_id,
                            )
                        )
                    await conn.execute(
                        t.actions.update()
                        .where(
                            t.actions.c.chat_id == gid,
                            t.actions.c.actor_id == previous["actor_id"],
                            t.actions.c.state == "planned",
                        )
                        .values(state="cancelled", error="manager_changed")
                    )
            await conn.execute(
                t.roles.update()
                .where(
                    t.roles.c.chat_id == gid,
                    t.roles.c.actor_id == target,
                    t.roles.c.revoked_at.is_(None),
                )
                .values(revoked_at=now())
            )
            if role != "remove":
                await conn.execute(
                    t.roles.insert().values(
                        chat_id=gid,
                        actor_id=target,
                        role=role,
                        granted_by=scope.actor_id,
                    )
                )
            await conn.execute(
                t.actions.update()
                .where(
                    t.actions.c.chat_id == gid,
                    t.actions.c.actor_id == target,
                    t.actions.c.state == "planned",
                )
                .values(state="cancelled", error="role_changed")
            )
            return {"message": "Локальная роль обновлена."}
        if kind == "retry_job":
            changed = await conn.execute(
                t.jobs.update()
                .where(
                    t.jobs.c.chat_id == gid,
                    t.jobs.c.id == uuid.UUID(p["id"]),
                    t.jobs.c.state == "dead",
                )
                .values(state="pending", attempt=0, due_at=now())
            )
            return {
                "message": "Задание возвращено в очередь."
                if changed.rowcount
                else "Задание уже обработано."
            }
        if kind == "retry_action":
            action = (
                (
                    await conn.execute(
                        select(t.actions)
                        .where(
                            t.actions.c.chat_id == gid,
                            t.actions.c.id == uuid.UUID(p["id"]),
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if not action or action["state"] not in {"failed", "unknown", "in_flight"}:
                raise Conflict("Действие не требует повтора или сверки.")
            if action["state"] == "failed":
                binding = (
                    (
                        await conn.execute(
                            select(t.bindings).where(
                                t.bindings.c.chat_id == gid,
                                t.bindings.c.closed_at.is_(None),
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                if action["incident_id"]:
                    inc = (
                        (
                            await conn.execute(
                                select(t.incidents).where(
                                    t.incidents.c.id == action["incident_id"]
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                    if inc["version"] != action["object_version"]:
                        raise Conflict(
                            "Редакция карточки изменилась. Используйте актуальное решение."
                        )
                await conn.execute(
                    t.actions.update()
                    .where(t.actions.c.id == action["id"])
                    .values(
                        state="planned",
                        actor_id=scope.actor_id,
                        policy_version=settings["version"],
                        binding_version=binding["version"],
                        updated_at=now(),
                    )
                )
            await enqueue(
                conn,
                "action",
                "reconcile:" + op["token"],
                {"id": str(action["id"])},
                gid,
                priority=1,
            )
            return {
                "message": "Проверка действия поставлена в очередь. Неизвестный результат будет только сверяться."
            }
        if kind == "import_trust":
            changed = 0
            for row in p["rows"]:
                expires = (
                    datetime.fromisoformat(row["expires_at"])
                    if row.get("expires_at")
                    else None
                )
                if expires and (expires.tzinfo is None or expires <= now()):
                    raise Conflict(
                        "Срок доверия из файла уже истёк. Проверьте импорт заново."
                    )
                uid = await ensure_user(conn, row["telegram_user_id"])
                member = await lock_member(conn, gid, uid)
                await expire_trust(conn, gid, uid)
                member = await lock_member(conn, gid, uid)
                if not await active_list(conn, t.trust, gid, uid):
                    await conn.execute(
                        t.trust.insert().values(
                            chat_id=gid,
                            user_id=uid,
                            source="import",
                            reason=row["reason"],
                            actor_id=scope.actor_id,
                            cycle=member["cycle"],
                            expires_at=expires,
                        )
                    )
                    changed += 1
            return {"message": "Добавлено локальных доверий: " + str(changed)}
        incident = None
        if kind in {"label", "retry_check", "restore", "delete"}:
            incident = (
                (
                    await conn.execute(
                        select(t.incidents).where(
                            t.incidents.c.id == uuid.UUID(p["id"]),
                            t.incidents.c.chat_id == gid,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not incident:
                raise Denied("Карточка не принадлежит этой группе.")
            uid = incident["user_id"]
        else:
            uid = int(p["user_id"])
            if not (
                await conn.execute(
                    select(t.memberships.c.id).where(
                        t.memberships.c.chat_id == gid, t.memberships.c.user_id == uid
                    )
                )
            ).first():
                raise Denied("Участник не найден в этой группе.")
        member = await lock_member(conn, gid, uid) if uid else None
        if incident:
            incident = dict(
                (
                    await conn.execute(
                        select(t.incidents)
                        .where(t.incidents.c.id == incident["id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .one()
            )
            if incident["version"] != op["object_version"]:
                raise Conflict(
                    "Другой модератор или новая редакция уже изменили карточку."
                )
        elif member["version"] != op["object_version"]:
            raise Conflict("Профиль изменился. Проверьте действие заново.")
        source = None
        if p.get("source_incident"):
            source = (
                (
                    await conn.execute(
                        select(t.incidents)
                        .where(
                            t.incidents.c.id == uuid.UUID(p["source_incident"]),
                            t.incidents.c.chat_id == gid,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if (
                not source
                or source["user_id"] != uid
                or source["version"] != p["source_version"]
            ):
                raise Conflict("Карточка изменена. Откройте актуальный результат.")
        reason = p.get("reason", "Решение модератора")[:1000]
        if kind == "delete":
            if incident["legacy"]:
                raise Conflict(
                    "Исторические сообщения не удаляются из старой карточки."
                )
            items = (
                (
                    await conn.execute(
                        select(t.items.c.telegram_message_id).where(
                            t.items.c.chat_id == gid,
                            t.items.c.incident_key == incident["incident_key"],
                        )
                    )
                )
                .scalars()
                .all()
            )
            for mid in items:
                already = (
                    await conn.execute(
                        select(t.actions.c.id).where(
                            t.actions.c.chat_id == gid,
                            t.actions.c.incident_id == incident["id"],
                            t.actions.c.kind == "delete",
                            t.actions.c.payload["message_id"].as_integer() == mid,
                            t.actions.c.state.in_(
                                ["planned", "in_flight", "succeeded", "unknown"]
                            ),
                        )
                    )
                ).first()
                if already:
                    continue
                await plan_action(
                    conn,
                    settings,
                    "delete",
                    {"message_id": mid, "reason": reason},
                    f"manual-delete:{op['token']}:{mid}",
                    incident=incident,
                    user_id=uid,
                    actor=scope.actor_id,
                )
        elif kind == "restore":
            if incident["human_label"] != "NOT_SCAM":
                raise Conflict(
                    "Сначала подтвердите, что публикация не является скамом."
                )
            rows = (
                (
                    await conn.execute(
                        select(t.revisions.c.text, t.revisions.c.ocr_text)
                        .join(
                            t.items,
                            (t.items.c.id == t.revisions.c.item_id)
                            & (t.items.c.revision == t.revisions.c.revision),
                        )
                        .where(
                            t.items.c.chat_id == gid,
                            t.items.c.incident_key == incident["incident_key"],
                        )
                        .order_by(t.items.c.telegram_message_id)
                    )
                )
                .mappings()
                .all()
            )
            content = "\n\n".join(
                r["text"]
                + ("\nТекст изображения: " + r["ocr_text"] if r["ocr_text"] else "")
                for r in rows
            )
            if not content.strip():
                raise Conflict("Нет сохранённого текста для восстановления.")
            await plan_action(
                conn,
                settings,
                "restore",
                {"text": content, "reason": reason},
                "restore:" + op["token"],
                incident=incident,
                user_id=uid,
                actor=scope.actor_id,
            )
        elif kind == "retry_check":
            if incident["legacy"]:
                raise Conflict(
                    "Исторические сообщения доступны для ручной разметки; повторная модельная проверка не создаёт санкций."
                )
            version = incident["version"] + 1
            await conn.execute(
                t.actions.update()
                .where(
                    t.actions.c.incident_id == incident["id"],
                    t.actions.c.state == "planned",
                )
                .values(state="cancelled", error="recheck_requested")
            )
            await conn.execute(
                t.incidents.update()
                .where(t.incidents.c.id == incident["id"])
                .values(version=version, status="pending", updated_at=now())
            )
            await enqueue(
                conn,
                "check",
                f"recheck:{incident['id']}:{version}",
                {"id": str(incident["id"]), "version": version},
                gid,
            )
        elif kind == "label":
            if p["label"] not in {"SCAM", "NOT_SCAM"}:
                raise Conflict("Недопустимая оценка.")
            if incident["human_label"] == p["label"]:
                return {"message": "Такая оценка уже сохранена."}
            incident.update(
                version=incident["version"] + 1,
                human_label=p["label"],
                status="reviewed",
            )
            await conn.execute(
                t.incidents.update()
                .where(t.incidents.c.id == incident["id"])
                .values(
                    version=incident["version"],
                    human_label=p["label"],
                    status="reviewed",
                    updated_at=now(),
                )
            )
            await conn.execute(
                t.decisions.insert().values(
                    chat_id=gid,
                    incident_id=incident["id"],
                    actor_id=scope.actor_id,
                    version=incident["version"],
                    label=p["label"],
                    reason=reason,
                )
            )
            await conn.execute(
                t.actions.update()
                .where(
                    t.actions.c.incident_id == incident["id"],
                    t.actions.c.state == "planned",
                )
                .values(state="cancelled", error="human_decision")
            )
            if p["label"] == "NOT_SCAM":
                await cancel_strike(conn, incident, scope.actor_id)
                # Restrictions are removed only by an explicit unban/unmute action.
            elif p.get("enforce") and not incident["legacy"]:
                # Confirmed plan explicitly includes deletion and one incident contribution.
                messages = (
                    await conn.execute(
                        select(t.items.c.telegram_message_id).where(
                            t.items.c.chat_id == gid,
                            t.items.c.incident_key == incident["incident_key"],
                        )
                    )
                ).scalars()
                for message_id in messages:
                    await plan_action(
                        conn,
                        settings,
                        "delete",
                        {"message_id": message_id},
                        f"manualdelete:{op['token']}:{message_id}",
                        incident=incident,
                        user_id=uid,
                        actor=scope.actor_id,
                    )
                await contribute_strike(conn, incident, settings, scope.actor_id)
            await apply_credit(conn, incident, settings)
            await card(conn, incident, "human")
        elif kind in {"trust_add", "trust_remove", "deny_add", "deny_remove"}:
            if kind == "trust_remove":
                if p.get("grant_id"):
                    current_grant = (
                        await conn.execute(
                            select(t.trust.c.id).where(
                                t.trust.c.chat_id == gid,
                                t.trust.c.user_id == uid,
                                t.trust.c.id == uuid.UUID(p["grant_id"]),
                                t.trust.c.revoked_at.is_(None),
                                (
                                    t.trust.c.expires_at.is_(None)
                                    | (t.trust.c.expires_at > now())
                                ),
                            )
                        )
                    ).scalar_one_or_none()
                    if not current_grant:
                        raise Conflict(
                            "Эта выдача доверия уже не действует. Откройте профиль заново."
                        )
                await remove_trust(conn, gid, uid, scope.actor_id, reason)
            elif kind == "deny_remove":
                await conn.execute(
                    t.deny.update()
                    .where(
                        t.deny.c.chat_id == gid,
                        t.deny.c.user_id == uid,
                        t.deny.c.revoked_at.is_(None),
                    )
                    .values(revoked_at=now())
                )
            else:
                table = t.trust if kind == "trust_add" else t.deny
                await expire_trust(conn, gid, uid)
                member = await lock_member(conn, gid, uid)
                await conn.execute(
                    table.update()
                    .where(
                        table.c.chat_id == gid,
                        table.c.user_id == uid,
                        table.c.revoked_at.is_(None),
                        table.c.expires_at <= now(),
                    )
                    .values(revoked_at=now())
                )
                if not await active_list(conn, table, gid, uid):
                    values = dict(
                        chat_id=gid, user_id=uid, actor_id=scope.actor_id, reason=reason
                    )
                    if p.get("days"):
                        values["expires_at"] = now() + timedelta(
                            days=min(3650, max(1, int(p["days"])))
                        )
                    if kind == "trust_add":
                        values.update(source="manual", cycle=member["cycle"])
                    await conn.execute(table.insert().values(**values))
            await conn.execute(
                t.member_states.update()
                .where(
                    t.member_states.c.chat_id == gid, t.member_states.c.user_id == uid
                )
                .values(version=t.member_states.c.version + 1)
            )
        elif kind == "reset_strikes":
            await conn.execute(
                t.strikes.insert().values(
                    chat_id=gid,
                    user_id=uid,
                    delta=-member["balance"],
                    kind="adjustment",
                    actor_id=scope.actor_id,
                    reason=reason,
                )
            )
            await conn.execute(
                t.strikes.update()
                .where(
                    t.strikes.c.chat_id == gid,
                    t.strikes.c.user_id == uid,
                    t.strikes.c.kind == "incident",
                    t.strikes.c.revoked_at.is_(None),
                )
                .values(revoked_at=now())
            )
            await conn.execute(
                t.incidents.update()
                .where(t.incidents.c.chat_id == gid, t.incidents.c.user_id == uid)
                .values(active_strike=False)
            )
            await conn.execute(
                t.member_states.update()
                .where(
                    t.member_states.c.chat_id == gid, t.member_states.c.user_id == uid
                )
                .values(balance=0, version=member["version"] + 1)
            )
            await conn.execute(
                t.actions.update()
                .where(
                    t.actions.c.chat_id == gid,
                    t.actions.c.user_id == uid,
                    t.actions.c.actor_id.is_(None),
                    t.actions.c.state == "planned",
                )
                .values(state="cancelled", error="strikes_reset")
            )
        else:
            if source and kind in {"ban", "mute"}:
                previous = (
                    (
                        await conn.execute(
                            select(t.actions)
                            .where(
                                t.actions.c.incident_id == source["id"],
                                t.actions.c.chat_id == gid,
                                t.actions.c.kind.in_(
                                    [kind, "unban" if kind == "ban" else "unmute"]
                                ),
                                t.actions.c.state.in_(
                                    ["planned", "in_flight", "succeeded", "unknown"]
                                ),
                            )
                            .order_by(
                                t.actions.c.created_at.desc(), t.actions.c.id.desc()
                            )
                            .limit(1)
                        )
                    )
                    .mappings()
                    .first()
                )
                if previous and previous["kind"] == kind:
                    raise Conflict(
                        "Это действие уже принято. Откройте актуальную карточку и проверьте результат."
                    )
            if (
                kind == "mute"
                and not 60
                <= int(
                    p.get(
                        "seconds",
                        Policy.model_validate(settings["policy"]).mute_seconds,
                    )
                )
                <= 366 * 86400
            ):
                raise Conflict(
                    "Длительность мута должна быть от 60 секунд до 366 дней."
                )
            await plan_action(
                conn,
                settings,
                kind,
                {
                    "acknowledged_deny_id": p.get("acknowledged_deny_id"),
                    "seconds": p.get(
                        "seconds",
                        Policy.model_validate(settings["policy"]).mute_seconds,
                    ),
                },
                "manual:" + op["token"],
                incident=source,
                user_id=uid,
                actor=scope.actor_id,
            )
            await conn.execute(
                t.member_states.update()
                .where(
                    t.member_states.c.chat_id == gid, t.member_states.c.user_id == uid
                )
                .values(version=member["version"] + 1)
            )
        if kind in {"trust_add", "trust_remove"}:
            # Refresh already delivered cards; do not create alerts for unseen history.
            affected = (
                (
                    await conn.execute(
                        select(t.incidents)
                        .where(
                            t.incidents.c.chat_id == gid,
                            t.incidents.c.user_id == uid,
                            t.incidents.c.id.in_(
                                select(t.cards.c.incident_id).where(
                                    t.cards.c.chat_id == gid,
                                    t.cards.c.binding_version == scope.binding_version,
                                    t.cards.c.admin_chat_id == scope.admin_chat_id,
                                )
                            ),
                        )
                        .order_by(t.incidents.c.created_at.desc())
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
            for affected_incident in affected:
                await card(conn, affected_incident, "trust:" + op["token"])
        elif source:
            await card(conn, source, "member:" + op["token"])
        elif incident and kind in {"delete", "restore"}:
            await card(conn, incident, "planned:" + op["token"])
        messages = {
            "trust_add": "Участник в вайтлисте этой рабочей группы. Бан, мут и страйки не изменены.",
            "trust_remove": "Доверие снято только в этой рабочей группе. Начат новый цикл автовайтлиста.",
            "deny_add": "Участник в блэклисте этой рабочей группы. Это не подтверждение бана в Telegram.",
            "deny_remove": "Участник убран из блэклиста этой рабочей группы. Бан и мут в Telegram не снимаются этим действием.",
            "reset_strikes": "Страйки сброшены в этой рабочей группе. Доверие, бан и мут не изменены.",
        }
        return {
            "message": messages.get(
                kind,
                "Решение сохранено. Выполнение действий Telegram отслеживается отдельно.",
            )
        }

    async def detail(self, scope, incident_id):
        from app.v2.card_state import member_card_state

        async with self.engine.connect() as conn:
            await assert_scope(conn, scope, "read")
            inc = (
                (
                    await conn.execute(
                        select(
                            t.incidents,
                            t.users.c.telegram_user_id,
                            t.users.c.username,
                            t.users.c.first_name,
                            t.users.c.last_name,
                            t.chats.c.title.label("group_title"),
                            t.group_settings.c.policy["timezone"]
                            .as_string()
                            .label("timezone"),
                        )
                        .outerjoin(t.users, t.users.c.id == t.incidents.c.user_id)
                        .join(t.chats, t.chats.c.id == t.incidents.c.chat_id)
                        .join(
                            t.group_settings,
                            t.group_settings.c.chat_id == t.incidents.c.chat_id,
                        )
                        .where(
                            t.incidents.c.chat_id == scope.chat_id,
                            t.incidents.c.id == uuid.UUID(str(incident_id)),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not inc:
                raise Denied("Карточка недоступна в этой группе.")
            texts = (
                (
                    await conn.execute(
                        select(
                            t.revisions.c.text,
                            t.revisions.c.ocr_text,
                            t.revisions.c.content,
                            t.items.c.telegram_message_id,
                        )
                        .join(
                            t.items,
                            (t.items.c.id == t.revisions.c.item_id)
                            & (t.items.c.revision == t.revisions.c.revision),
                        )
                        .where(
                            t.items.c.chat_id == scope.chat_id,
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
                            t.actions.c.kind,
                            t.actions.c.state,
                            t.actions.c.error,
                            t.actions.c.payload,
                        )
                        .where(
                            t.actions.c.chat_id == scope.chat_id,
                            t.actions.c.incident_id == inc["id"],
                        )
                        .order_by(t.actions.c.created_at, t.actions.c.id)
                    )
                )
                .mappings()
                .all()
            )
            decisions = (
                (
                    await conn.execute(
                        select(t.decisions)
                        .where(
                            t.decisions.c.chat_id == scope.chat_id,
                            t.decisions.c.incident_id == inc["id"],
                        )
                        .order_by(t.decisions.c.created_at.desc())
                        .limit(20)
                    )
                )
                .mappings()
                .all()
            )
            telegram_chat = (
                await conn.execute(
                    select(t.chats.c.telegram_chat_id).where(
                        t.chats.c.id == scope.chat_id
                    )
                )
            ).scalar_one()
            deleted = {
                r["payload"].get("message_id")
                for r in actions
                if r["kind"] == "delete"
                and r["state"] in {"succeeded", "in_flight", "unknown"}
            }
            links = [
                f"https://t.me/c/{str(telegram_chat)[4:]}/{r['telegram_message_id']}"
                for r in texts
                if str(telegram_chat).startswith("-100")
                and r["telegram_message_id"] not in deleted
            ]
            return {
                **inc,
                **await member_card_state(conn, inc),
                "texts": [dict(r) for r in texts],
                "actions": [dict(r) for r in actions],
                "decisions": [dict(r) for r in decisions],
                "message_links": links,
            }

    async def members(self, scope, query="", offset=0):
        """Only known members of this group; a name never identifies someone by itself."""
        query = query.strip()
        if len(query) > 100 or not 0 <= offset <= 100000:
            raise Conflict("Сократите запрос до 100 символов или начните поиск заново.")
        async with self.engine.connect() as conn:
            await assert_scope(conn, scope, "read")
            stmt = (
                select(t.users)
                .join(t.memberships, t.memberships.c.user_id == t.users.c.id)
                .where(t.memberships.c.chat_id == scope.chat_id)
            )
            if query:
                if query.isascii() and query.isdigit():
                    if int(query) > 9223372036854775807:
                        raise Conflict("Некорректный Telegram ID.")
                    condition = t.users.c.telegram_user_id == int(query)
                elif query.startswith("@"):
                    condition = func.lower(t.users.c.username) == query[1:].lower()
                else:
                    name = func.concat_ws(
                        " ", t.users.c.first_name, t.users.c.last_name
                    )
                    condition = or_(
                        name.icontains(query, autoescape=True),
                        t.users.c.username.icontains(query, autoescape=True),
                    )
                stmt = stmt.where(condition)
            rows = (
                (
                    await conn.execute(
                        stmt.order_by(t.memberships.c.id.desc())
                        .offset(offset)
                        .limit(13)
                    )
                )
                .mappings()
                .all()
            )
            return [dict(r) for r in rows[:12]], len(rows) > 12

    async def profile(self, scope, user_id):
        async with self.engine.connect() as conn:
            await assert_scope(conn, scope, "read")
            row = (
                (
                    await conn.execute(
                        select(
                            t.member_states,
                            t.users.c.telegram_user_id,
                            t.users.c.username,
                            t.users.c.first_name,
                            t.users.c.last_name,
                        )
                        .join(t.users, t.users.c.id == t.member_states.c.user_id)
                        .where(
                            t.member_states.c.chat_id == scope.chat_id,
                            t.member_states.c.user_id == user_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if not row:
                raise Denied("Участник не найден в этой группе.")
            count = (
                await conn.execute(
                    select(func.count())
                    .select_from(t.credits)
                    .where(
                        t.credits.c.chat_id == scope.chat_id,
                        t.credits.c.user_id == user_id,
                        t.credits.c.cycle == row["cycle"],
                        t.credits.c.eligible.is_(True),
                    )
                )
            ).scalar_one()
            trust = await active_list(conn, t.trust, scope.chat_id, user_id)
            deny = await active_list(conn, t.deny, scope.chat_id, user_id)
            return {
                **row,
                "credits": count,
                "trust": bool(trust),
                "deny": bool(deny),
                "trust_entry": dict(trust) if trust else None,
                "deny_entry": dict(deny) if deny else None,
            }

    async def page(self, scope, section, cursor=None, user_id=None, filters=None):
        async with self.engine.connect() as conn:
            await assert_scope(conn, scope, "read")
            table = {
                "history": t.incidents,
                "review": t.incidents,
                "trust": t.trust,
                "deny": t.deny,
                "audit": t.audit,
                "actions": t.actions,
                "jobs": t.jobs,
                "roles": t.roles,
            }.get(section)
            if table is None:
                raise Conflict("Неизвестный раздел.")
            stmt = select(table).where(table.c.chat_id == scope.chat_id)
            if section in {"history", "review", "trust", "deny"}:
                stmt = stmt.add_columns(
                    t.users.c.telegram_user_id,
                    t.users.c.username,
                    t.users.c.first_name,
                    t.users.c.last_name,
                ).outerjoin(t.users, t.users.c.id == table.c.user_id)
            if section == "review":
                stmt = stmt.where(t.incidents.c.status.in_(["review", "error"]))
            if section in {"trust", "deny", "roles"}:
                stmt = stmt.where(table.c.revoked_at.is_(None))
            if section in {"trust", "deny"}:
                stmt = stmt.where(
                    table.c.expires_at.is_(None) | (table.c.expires_at > now())
                )
            if section == "jobs":
                stmt = stmt.where(t.jobs.c.state.in_(["dead", "pending", "running"]))
            if user_id and "user_id" in table.c:
                stmt = stmt.where(table.c.user_id == user_id)
            if section in {"history", "review"} and filters:
                if filters.get("status"):
                    stmt = stmt.where(table.c.status == filters["status"])
                if filters.get("category"):
                    stmt = stmt.where(table.c.category == filters["category"])
                if filters.get("days"):
                    stmt = stmt.where(
                        table.c.created_at
                        >= now() - timedelta(days=min(365, int(filters["days"])))
                    )
                if filters.get("label") == "SCAM":
                    stmt = stmt.where(
                        (table.c.human_label == "SCAM")
                        | (
                            table.c.human_label.is_(None)
                            & (table.c.model_label == "SCAM")
                        )
                    )
                if filters.get("label") == "NOT_SCAM":
                    stmt = stmt.where(
                        (table.c.human_label == "NOT_SCAM")
                        | (
                            table.c.human_label.is_(None)
                            & (table.c.model_label == "OK")
                        )
                    )
                if filters.get("user_id"):
                    stmt = stmt.where(table.c.user_id == int(filters["user_id"]))
            if cursor:
                prior = (
                    await conn.execute(
                        select(table.c.created_at, table.c.id).where(
                            table.c.chat_id == scope.chat_id,
                            table.c.id == uuid.UUID(cursor),
                        )
                    )
                ).first()
                if not prior:
                    raise Denied("Недействительная страница.")
                stmt = stmt.where(
                    tuple_(table.c.created_at, table.c.id) < tuple_(*prior)
                )
            rows = [
                dict(r)
                for r in (
                    await conn.execute(
                        stmt.order_by(
                            table.c.created_at.desc(), table.c.id.desc()
                        ).limit(11)
                    )
                ).mappings()
            ]
            return rows[:10], str(rows[9]["id"]) if len(rows) > 10 else None

    async def export(self, scope, section, filters=None):
        async with self.engine.begin() as conn:
            await assert_scope(conn, scope, "export")
            if section in {"trust", "deny"}:
                table = t.trust if section == "trust" else t.deny
                stmt = (
                    select(
                        t.users.c.telegram_user_id,
                        table.c.reason,
                        table.c.created_at,
                        table.c.expires_at,
                    )
                    .join(t.users, t.users.c.id == table.c.user_id)
                    .where(
                        table.c.chat_id == scope.chat_id,
                        table.c.revoked_at.is_(None),
                        table.c.expires_at.is_(None) | (table.c.expires_at > now()),
                    )
                )
            elif section == "actions":
                stmt = select(
                    t.actions.c.id,
                    t.actions.c.created_at,
                    t.actions.c.user_id,
                    t.actions.c.incident_id,
                    t.actions.c.kind,
                    t.actions.c.state,
                    t.actions.c.actor_id,
                    t.actions.c.error,
                ).where(t.actions.c.chat_id == scope.chat_id)
                if filters and filters.get("days"):
                    stmt = stmt.where(
                        t.actions.c.created_at
                        >= now() - timedelta(days=min(365, int(filters["days"])))
                    )
            elif section == "history":
                stmt = select(
                    t.incidents.c.id,
                    t.incidents.c.created_at,
                    t.incidents.c.user_id,
                    t.incidents.c.model_label,
                    t.incidents.c.human_label,
                    t.incidents.c.reason,
                    t.incidents.c.status,
                ).where(t.incidents.c.chat_id == scope.chat_id)
                if filters:
                    if filters.get("status"):
                        stmt = stmt.where(t.incidents.c.status == filters["status"])
                    if filters.get("category"):
                        stmt = stmt.where(t.incidents.c.category == filters["category"])
                    if filters.get("days"):
                        stmt = stmt.where(
                            t.incidents.c.created_at
                            >= now() - timedelta(days=min(365, int(filters["days"])))
                        )
                    if filters.get("user_id"):
                        stmt = stmt.where(
                            t.incidents.c.user_id == int(filters["user_id"])
                        )
                    if filters.get("label") == "SCAM":
                        stmt = stmt.where(
                            (t.incidents.c.human_label == "SCAM")
                            | (
                                t.incidents.c.human_label.is_(None)
                                & (t.incidents.c.model_label == "SCAM")
                            )
                        )
                    if filters.get("label") == "NOT_SCAM":
                        stmt = stmt.where(
                            (t.incidents.c.human_label == "NOT_SCAM")
                            | (
                                t.incidents.c.human_label.is_(None)
                                & (t.incidents.c.model_label == "OK")
                            )
                        )
            else:
                raise Conflict("Экспорт доступен для истории и списков.")
            result = await conn.execute(stmt.limit(10001))
            fields, rows = list(result.keys()), result.all()
            if len(rows) > 10000:
                raise Conflict(
                    "Более 10 000 записей. Выберите период или участника в истории и повторите экспорт."
                )
            await audit(
                conn,
                scope.chat_id,
                scope.actor_id,
                "operator.export",
                section=section,
                filters=filters or {},
                count=len(rows),
            )
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(fields)
        writer.writerows(
            [
                csv_safe(
                    "'" + str(v)
                    if fields[i] in {"telegram_user_id", "actor_id"} and v is not None
                    else v
                )
                for i, v in enumerate(row)
            ]
            for row in rows
        )
        return output.getvalue().encode("utf-8-sig")
