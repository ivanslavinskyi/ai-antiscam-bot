"""Explicit data migration; never imports or calls Telegram/OpenAI clients."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import create_engine, select, text, func
from sqlalchemy.engine import make_url
from app.v2 import tables as t
from app.v2.domain import Policy, credit_eligible, now

NAMESPACE = uuid.UUID("8f0590b1-b040-46a6-8ab2-3dc288ec9cc8")


def identifier(kind, *parts):
    return uuid.uuid5(NAMESPACE, kind + ":" + ":".join(map(str, parts)))


def digest(value):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(raw.encode()).hexdigest()


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_id: int = Field(gt=0)
    primary_chat_id: int = Field(gt=0)
    primary_telegram_id: int = Field(lt=0)
    primary_admin_id: int = Field(lt=0)
    source_group_ids: list[int]
    archived_group_ids: list[int]
    owner_ids: list[int] = Field(min_length=1)
    local_roles: dict[int, str] = Field(default_factory=dict)
    primary_policy: Policy
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: str = "rehearsal"

    @model_validator(mode="after")
    def coherent(self):
        if set(self.source_group_ids) != {
            self.primary_chat_id,
            *self.archived_group_ids,
        }:
            raise ValueError("Every source group requires an explicit disposition.")
        if self.primary_chat_id in self.archived_group_ids:
            raise ValueError("Primary group cannot be archived.")
        if self.primary_admin_id == self.primary_telegram_id:
            raise ValueError("Working and admin groups must differ.")
        if any(i <= 0 for i in self.owner_ids) or any(i <= 0 for i in self.local_roles):
            raise ValueError("Telegram user IDs must be positive.")
        if not set(self.local_roles.values()) <= {"manager", "moderator", "observer"}:
            raise ValueError("Invalid local role.")
        if self.purpose not in {"rehearsal", "production"}:
            raise ValueError("Invalid purpose.")
        return self


def source_rows(conn):
    return {
        table.name: [
            dict(r) for r in conn.execute(select(table).order_by(table.c.id)).mappings()
        ]
        for table in (t.chats, t.users, t.memberships, t.legacy_messages)
    }


def inspect_source(conn, manifest: Manifest):
    data = source_rows(conn)
    groups = {r["id"]: r for r in data["chats"]}
    if set(groups) != set(manifest.source_group_ids):
        raise ValueError("Source groups differ from manifest.")
    if (
        groups[manifest.primary_chat_id]["telegram_chat_id"]
        != manifest.primary_telegram_id
    ):
        raise ValueError("Primary Telegram ID differs from manifest.")
    if manifest.primary_admin_id in {r["telegram_chat_id"] for r in groups.values()}:
        raise ValueError("An existing working group cannot be used as the admin group.")
    global_ids = {r["id"] for r in data["users"] if r["is_global_whitelisted"]}
    member_ids = {
        r["user_id"]
        for r in data["chat_members"]
        if r["chat_id"] == manifest.primary_chat_id
    }
    local_ids = {
        r["user_id"]
        for r in data["chat_members"]
        if r["chat_id"] == manifest.primary_chat_id and r["is_whitelisted"]
    }
    expected_trust = sorted(global_ids | local_ids)
    report = {
        "source_counts": {n: len(rows) for n, rows in data.items()},
        "source_hashes": {n: digest(rows) for n, rows in data.items()},
        "expected_trust_ids": expected_trust,
        "missing_memberships": sorted(set(expected_trust) - member_ids),
        "legacy_balance": sum(r["strike_count"] for r in data["chat_members"]),
        "primary_balance": sum(
            r["strike_count"]
            for r in data["chat_members"]
            if r["chat_id"] == manifest.primary_chat_id
        ),
        "archive_sha256": manifest.archive_sha256,
    }
    if any(r["strike_count"] < 0 for r in data["chat_members"]):
        raise ValueError("Negative legacy strikes require explicit review.")
    return data, report


def bulk(conn, table, rows):
    for offset in range(0, len(rows), 1000):
        conn.execute(table.insert(), rows[offset : offset + 1000])


def import_data(conn, manifest: Manifest):
    conn.execute(text("SELECT pg_advisory_xact_lock(71392142)"))
    run_id = digest(manifest.model_dump(mode="json"))
    existing = (
        conn.execute(select(t.migrations).where(t.migrations.c.id == run_id))
        .mappings()
        .first()
    )
    if existing and existing["state"] == "verified":
        return {**existing["report"], "already_applied": True}
    if conn.execute(select(func.count()).select_from(t.migrations)).scalar_one():
        raise ValueError(
            "Another migration run exists; automatic reimport is forbidden."
        )
    data, report = inspect_source(conn, manifest)
    conn.execute(
        t.migrations.insert().values(
            id=run_id,
            state="running",
            manifest=manifest.model_dump(mode="json"),
            report=report,
        )
    )
    primary = manifest.primary_chat_id
    for row in data["chats"]:
        conn.execute(
            t.group_settings.insert().values(
                chat_id=row["id"],
                lifecycle="connected" if row["id"] == primary else "archived",
                mode="auto" if row["id"] == primary else "paused",
                policy=(
                    manifest.primary_policy if row["id"] == primary else Policy()
                ).model_dump(),
            )
        )
        conn.execute(
            t.aliases.insert().values(
                telegram_chat_id=row["telegram_chat_id"], chat_id=row["id"]
            )
        )
    conn.execute(
        t.bindings.insert().values(
            chat_id=primary,
            admin_chat_id=manifest.primary_admin_id,
            version=1,
            confirmed_by=manifest.owner_ids[0],
        )
    )
    for actor in set(manifest.owner_ids):
        conn.execute(
            t.roles.insert().values(
                actor_id=actor, role="owner", granted_by=manifest.owner_ids[0]
            )
        )
    for actor, role in manifest.local_roles.items():
        conn.execute(
            t.roles.insert().values(
                chat_id=primary,
                actor_id=actor,
                role=role,
                granted_by=manifest.owner_ids[0],
            )
        )
    for user_id in report["missing_memberships"]:
        conn.execute(
            t.memberships.insert().values(
                chat_id=primary,
                user_id=user_id,
                is_whitelisted=False,
                is_blacklisted=False,
                strike_count=0,
            )
        )
    memberships = [dict(r) for r in conn.execute(select(t.memberships)).mappings()]
    bulk(
        conn,
        t.member_states,
        [
            dict(
                chat_id=r["chat_id"],
                user_id=r["user_id"],
                legacy_balance=r["strike_count"],
                balance=r["strike_count"],
                legacy_last_strike_at=r["last_strike_at"],
            )
            for r in memberships
        ],
    )
    global_ids = {r["id"] for r in data["users"] if r["is_global_whitelisted"]}
    trusts = {}
    for row in data["chat_members"]:
        if row["is_whitelisted"]:
            trusts[(row["chat_id"], row["user_id"])] = "legacy_local"
    for uid in global_ids:
        trusts[(primary, uid)] = "legacy_global"
    bulk(
        conn,
        t.trust,
        [
            dict(
                chat_id=g,
                user_id=u,
                source=source,
                reason="Импортировано; первоначальное происхождение неизвестно.",
            )
            for (g, u), source in trusts.items()
        ],
    )
    bulk(
        conn,
        t.deny,
        [
            dict(
                chat_id=r["chat_id"],
                user_id=r["user_id"],
                reason="Импортировано; первоначальное происхождение неизвестно.",
            )
            for r in memberships
            if r["is_blacklisted"]
        ],
    )
    user_telegram = {r["id"]: r["telegram_user_id"] for r in data["users"]}
    grouped = defaultdict(list)
    for row in data["messages"]:
        grouped[(row["chat_id"], row["telegram_message_id"])].append(row)
    (
        item_rows,
        revision_rows,
        incident_rows,
        check_rows,
        link_rows,
        credit_rows,
        decision_rows,
    ) = ([] for _ in range(7))
    for (chat_id, message_id), versions in grouped.items():
        versions.sort(key=lambda r: (r["created_at"], r["id"]))
        if len({r["user_id"] for r in versions}) != 1:
            raise ValueError("Conflicting legacy authors require explicit review.")
        human = [r for r in versions if r["human_label"]]
        human.sort(key=lambda r: (r["human_labeled_at"], r["id"]))
        if (
            len(human) > 1
            and human[-1]["human_labeled_at"] == human[-2]["human_labeled_at"]
            and human[-1]["human_label"] != human[-2]["human_label"]
        ):
            raise ValueError(
                "Conflicting legacy human decisions require explicit review."
            )
        chosen = human[-1] if human else versions[-1]
        item_id = identifier("item", chat_id, message_id)
        incident_id = identifier("incident", chat_id, message_id)
        key = f"message:{message_id}"
        item_rows.append(
            dict(
                id=item_id,
                chat_id=chat_id,
                telegram_message_id=message_id,
                user_id=chosen["user_id"],
                author_kind="user",
                revision=len(versions),
                incident_key=key,
                telegram_date=versions[0]["created_at"],
                first_received_at=versions[0]["created_at"],
            )
        )
        incident_rows.append(
            dict(
                id=incident_id,
                chat_id=chat_id,
                incident_key=key,
                user_id=chosen["user_id"],
                human_label=chosen["human_label"],
                model_label=chosen["model_label"],
                category=chosen["model_category"],
                confidence=chosen["model_confidence"],
                reason=chosen["model_reason"],
                status="legacy",
                legacy=True,
                created_at=versions[0]["created_at"],
            )
        )
        if chosen["human_label"]:
            decision_rows.append(
                dict(
                    chat_id=chat_id,
                    incident_id=incident_id,
                    actor_id=user_telegram[chosen["human_labeled_by"]],
                    version=1,
                    label=chosen["human_label"],
                    reason="Исходная ручная разметка",
                    created_at=chosen["human_labeled_at"],
                )
            )
        if credit_eligible(
            chosen["model_label"], chosen["human_label"], not chosen["skipped_reason"]
        ):
            credit_rows.append(
                dict(
                    chat_id=chat_id,
                    user_id=chosen["user_id"],
                    cycle=0,
                    incident_id=incident_id,
                    eligible=True,
                    source="legacy",
                )
            )
        for index, row in enumerate(versions, 1):
            rid = identifier("revision", row["id"])
            raw = row["model_raw_json"] or {}
            revision_rows.append(
                dict(
                    id=rid,
                    chat_id=chat_id,
                    item_id=item_id,
                    revision=index,
                    text=row["text"],
                    normalized=row["text"],
                    content_hash=digest(row["text"]),
                    content={"legacy_message_id": row["id"]},
                    created_at=row["created_at"],
                )
            )
            usage = raw.get("usage") or {}
            valid_usage = all(
                isinstance(usage.get(k), int) and usage[k] >= 0
                for k in ("prompt_tokens", "completion_tokens")
            )
            cost = (
                (usage["prompt_tokens"] * 0.15 + usage["completion_tokens"] * 0.6) / 1e6
                if valid_usage and str(row["model_version"]).startswith("gpt-4o-mini")
                else None
            )
            check_rows.append(
                dict(
                    chat_id=chat_id,
                    revision_id=rid,
                    incident_id=incident_id,
                    model_requested=row["model_version"],
                    model_reported=raw.get("model"),
                    status="legacy",
                    label=row["model_label"],
                    category=row["model_category"],
                    confidence=row["model_confidence"],
                    reason=row["model_reason"],
                    usage=usage,
                    source="legacy_provider_response",
                    cost_usd=cost,
                    created_at=row["created_at"],
                )
            )
            link_rows.append(
                dict(
                    legacy_message_id=row["id"],
                    chat_id=chat_id,
                    item_id=item_id,
                    revision_id=rid,
                    incident_id=incident_id,
                    source_hash=digest(row),
                )
            )
    for table, rows in (
        (t.items, item_rows),
        (t.revisions, revision_rows),
        (t.incidents, incident_rows),
        (t.checks, check_rows),
        (t.legacy_links, link_rows),
        (t.credits, credit_rows),
        (t.decisions, decision_rows),
    ):
        bulk(conn, table, rows)
    for name, original in data.items():
        table = t.metadata.tables[name]
        ids = [r["id"] for r in original]
        preserved = [
            dict(r)
            for r in conn.execute(
                select(table).where(table.c.id.in_(ids)).order_by(table.c.id)
            ).mappings()
        ]
        if digest(preserved) != report["source_hashes"][name]:
            raise RuntimeError(f"Preservation check failed: {name}")
    actual_trust = sorted(
        conn.execute(
            select(t.trust.c.user_id).where(
                t.trust.c.chat_id == primary, t.trust.c.revoked_at.is_(None)
            )
        ).scalars()
    )
    if actual_trust != report["expected_trust_ids"]:
        raise RuntimeError("Whitelist preservation failed.")
    if (
        conn.execute(
            select(func.coalesce(func.sum(t.member_states.c.balance), 0))
        ).scalar_one()
        != report["legacy_balance"]
    ):
        raise RuntimeError("Strike preservation failed.")
    report.update(
        {
            "preserved_all_source_fields": True,
            "imported_messages": len(link_rows),
            "primary_trust": len(actual_trust),
            "new_memberships": len(report["missing_memberships"]),
            "memberships_after": len(memberships),
            "actions_created": 0,
            "primary_credits": len([r for r in credit_rows if r["chat_id"] == primary]),
            "verified_at": str(now()),
        }
    )
    conn.execute(
        t.migrations.update()
        .where(t.migrations.c.id == run_id)
        .values(state="verified", report=report, finished_at=now())
    )
    conn.execute(
        t.audit.insert().values(
            chat_id=primary,
            actor_id=manifest.owner_ids[0],
            kind="migration.verified",
            details={"run": run_id, "source_hashes": report["source_hashes"]},
        )
    )
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Explicit V2 preflight/import; no Telegram calls."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    manifest = Manifest.model_validate_json(args.manifest.read_text(encoding="utf-8"))
    url = make_url(os.environ["ANTISCAM_MIGRATION_DATABASE_URL"]).set(
        drivername="postgresql+psycopg"
    )
    engine = create_engine(url, hide_parameters=True)
    with engine.begin() as conn:
        if not args.apply:
            conn.execute(text("SET TRANSACTION READ ONLY"))
        report = (
            import_data(conn, manifest)
            if args.apply
            else inspect_source(conn, manifest)[1]
        )
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    engine.dispose()
    print("Verified report saved. No Telegram/OpenAI calls were made.")


if __name__ == "__main__":
    main()
