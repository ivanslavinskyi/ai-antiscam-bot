"""New tables share legacy metadata solely to resolve explicit foreign keys.

No create_all is called by runtime. Alembic creates only NEW_TABLES.
"""

import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from app.db.models import Base

metadata = Base.metadata
legacy_names = frozenset({"chats", "users", "chat_members", "messages"})
chats, users, memberships, legacy_messages = (
    metadata.tables[n] for n in ("chats", "users", "chat_members", "messages")
)


def uid():
    return Column("id", Uuid, primary_key=True, default=uuid.uuid4)


def timestamp(name="created_at", nullable=False):
    return Column(
        name,
        DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else text("now()"),
    )


def js(name, nullable=False):
    return Column(name, JSONB, nullable=nullable, server_default=text("'{}'::jsonb"))


def group():
    return Column("chat_id", Integer, ForeignKey("chats.id"), nullable=False)


group_settings = Table(
    "group_settings",
    metadata,
    Column("chat_id", Integer, ForeignKey("chats.id"), primary_key=True),
    Column("lifecycle", String(20), nullable=False, server_default="pending"),
    Column("mode", String(12), nullable=False, server_default="observe"),
    Column("health", String(16), nullable=False, server_default="healthy"),
    Column("health_reason", String(100)),
    Column("version", Integer, nullable=False, server_default="1"),
    js("policy"),
    timestamp("last_dispatched_at", nullable=True),
    timestamp("updated_at"),
    CheckConstraint("lifecycle IN ('pending','connected','archived','disconnected')"),
    CheckConstraint("mode IN ('observe','auto','paused')"),
)
aliases = Table(
    "group_id_aliases",
    metadata,
    Column("telegram_chat_id", BigInteger, primary_key=True),
    group(),
    timestamp(),
)
bindings = Table(
    "admin_bindings",
    metadata,
    uid(),
    group(),
    Column("admin_chat_id", BigInteger, nullable=False),
    Column("version", Integer, nullable=False),
    Column("confirmed_by", BigInteger, nullable=False),
    timestamp(),
    timestamp("closed_at", nullable=True),
)
Index(
    "uq_active_binding",
    bindings.c.chat_id,
    unique=True,
    postgresql_where=bindings.c.closed_at.is_(None),
)
roles = Table(
    "role_grants",
    metadata,
    uid(),
    Column("chat_id", Integer, ForeignKey("chats.id")),
    Column("actor_id", BigInteger, nullable=False),
    Column("role", String(16), nullable=False),
    Column("granted_by", BigInteger, nullable=False),
    timestamp(),
    timestamp("revoked_at", nullable=True),
    CheckConstraint(
        "(role='owner' AND chat_id IS NULL) OR "
        "(role IN ('manager','moderator','observer') AND chat_id IS NOT NULL)"
    ),
)
Index(
    "uq_local_role",
    roles.c.chat_id,
    roles.c.actor_id,
    unique=True,
    postgresql_where=roles.c.revoked_at.is_(None) & roles.c.chat_id.is_not(None),
)
Index(
    "uq_owner_role",
    roles.c.actor_id,
    unique=True,
    postgresql_where=roles.c.revoked_at.is_(None) & roles.c.chat_id.is_(None),
)
member_states = Table(
    "member_states",
    metadata,
    group(),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("legacy_balance", Integer, nullable=False, server_default="0"),
    Column("balance", Integer, nullable=False, server_default="0"),
    timestamp("legacy_last_strike_at", nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("cycle", Integer, nullable=False, server_default="0"),
    timestamp("cycle_started_at", nullable=True),
    UniqueConstraint("chat_id", "user_id", name="uq_member_state"),
    ForeignKeyConstraint(
        ["chat_id", "user_id"], ["chat_members.chat_id", "chat_members.user_id"]
    ),
    CheckConstraint("balance >= 0"),
)
trust = Table(
    "trust_grants",
    metadata,
    uid(),
    group(),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("source", String(24), nullable=False),
    Column("reason", Text, nullable=False),
    Column("actor_id", BigInteger),
    Column("cycle", Integer),
    Column("threshold", Integer),
    timestamp(),
    timestamp("expires_at", nullable=True),
    timestamp("revoked_at", nullable=True),
)
Index(
    "uq_active_trust",
    trust.c.chat_id,
    trust.c.user_id,
    unique=True,
    postgresql_where=trust.c.revoked_at.is_(None),
)
deny = Table(
    "deny_entries",
    metadata,
    uid(),
    group(),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("reason", Text, nullable=False),
    Column("actor_id", BigInteger),
    timestamp(),
    timestamp("expires_at", nullable=True),
    timestamp("revoked_at", nullable=True),
)
Index(
    "uq_active_deny",
    deny.c.chat_id,
    deny.c.user_id,
    unique=True,
    postgresql_where=deny.c.revoked_at.is_(None),
)
items = Table(
    "message_items",
    metadata,
    uid(),
    group(),
    Column("telegram_message_id", BigInteger, nullable=False),
    Column("user_id", Integer, ForeignKey("users.id")),
    Column("sender_chat_id", BigInteger),
    Column("author_kind", String(12), nullable=False),
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("media_group_id", String(100)),
    Column("incident_key", String(180), nullable=False),
    timestamp("telegram_date"),
    timestamp("first_received_at"),
    timestamp("edited_at", nullable=True),
    UniqueConstraint("chat_id", "telegram_message_id"),
    UniqueConstraint("chat_id", "id"),
)
revisions = Table(
    "message_revisions",
    metadata,
    uid(),
    group(),
    Column("item_id", Uuid, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("update_id", BigInteger),
    Column("text", Text, nullable=False),
    Column("normalized", Text, nullable=False, server_default=""),
    Column("ocr_text", Text, nullable=False, server_default=""),
    js("content"),
    Column("content_hash", String(64), nullable=False),
    Column("complete", Boolean, nullable=False, server_default="true"),
    Column("error", String(100)),
    timestamp(),
    UniqueConstraint("item_id", "revision"),
    UniqueConstraint("chat_id", "id"),
    ForeignKeyConstraint(
        ["chat_id", "item_id"], ["message_items.chat_id", "message_items.id"]
    ),
)
incidents = Table(
    "incidents",
    metadata,
    uid(),
    group(),
    Column("incident_key", String(180), nullable=False),
    Column("user_id", Integer, ForeignKey("users.id")),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("status", String(24), nullable=False, server_default="pending"),
    Column("human_label", String(16)),
    Column("model_label", String(16)),
    Column("category", String(32)),
    Column("confidence", Float),
    Column("reason", Text),
    Column("complete", Boolean, nullable=False, server_default="true"),
    Column("legacy", Boolean, nullable=False, server_default="false"),
    Column("active_strike", Boolean, nullable=False, server_default="false"),
    timestamp(),
    timestamp("updated_at"),
    UniqueConstraint("chat_id", "incident_key"),
    UniqueConstraint("chat_id", "id"),
)
checks = Table(
    "checks",
    metadata,
    uid(),
    group(),
    Column("revision_id", Uuid, nullable=False),
    Column("incident_id", Uuid, nullable=False),
    Column("model_requested", String(100)),
    Column("model_reported", String(100)),
    Column("prompt_version", String(64)),
    Column("policy_version", Integer),
    Column("status", String(24), nullable=False),
    Column("label", String(16)),
    Column("category", String(32)),
    Column("confidence", Float),
    Column("reason", Text),
    js("usage"),
    Column("cost_usd", Float),
    Column("latency_ms", Integer),
    Column("source", String(32), nullable=False),
    Column("error", String(100)),
    timestamp(),
    ForeignKeyConstraint(
        ["chat_id", "revision_id"],
        ["message_revisions.chat_id", "message_revisions.id"],
    ),
    ForeignKeyConstraint(
        ["chat_id", "incident_id"], ["incidents.chat_id", "incidents.id"]
    ),
)
legacy_links = Table(
    "legacy_message_links",
    metadata,
    Column("legacy_message_id", Integer, ForeignKey("messages.id"), primary_key=True),
    group(),
    Column("item_id", Uuid, nullable=False),
    Column("revision_id", Uuid, nullable=False),
    Column("incident_id", Uuid, nullable=False),
    Column("source_hash", String(64), nullable=False),
    ForeignKeyConstraint(
        ["chat_id", "item_id"], ["message_items.chat_id", "message_items.id"]
    ),
    ForeignKeyConstraint(
        ["chat_id", "revision_id"],
        ["message_revisions.chat_id", "message_revisions.id"],
    ),
    ForeignKeyConstraint(
        ["chat_id", "incident_id"], ["incidents.chat_id", "incidents.id"]
    ),
)
decisions = Table(
    "decisions",
    metadata,
    uid(),
    group(),
    Column("incident_id", Uuid, nullable=False),
    Column("actor_id", BigInteger),
    Column("version", Integer, nullable=False),
    Column("label", String(16), nullable=False),
    Column("reason", Text, nullable=False),
    timestamp(),
    ForeignKeyConstraint(
        ["chat_id", "incident_id"], ["incidents.chat_id", "incidents.id"]
    ),
)
strikes = Table(
    "strike_events",
    metadata,
    uid(),
    group(),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("incident_id", Uuid),
    Column("delta", Integer, nullable=False),
    Column("kind", String(24), nullable=False),
    Column("actor_id", BigInteger),
    Column("reason", Text, nullable=False),
    timestamp(),
    timestamp("revoked_at", nullable=True),
    ForeignKeyConstraint(
        ["chat_id", "incident_id"], ["incidents.chat_id", "incidents.id"]
    ),
)
Index(
    "uq_active_incident_strike",
    strikes.c.incident_id,
    unique=True,
    postgresql_where=strikes.c.revoked_at.is_(None) & (strikes.c.kind == "incident"),
)
credits = Table(
    "trust_credits",
    metadata,
    group(),
    Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
    Column("cycle", Integer, nullable=False),
    Column("incident_id", Uuid, nullable=False),
    Column("eligible", Boolean, nullable=False),
    Column("source", String(32), nullable=False),
    timestamp(),
    UniqueConstraint("chat_id", "user_id", "cycle", "incident_id"),
    ForeignKeyConstraint(
        ["chat_id", "incident_id"], ["incidents.chat_id", "incidents.id"]
    ),
)
inbox = Table(
    "inbox_updates",
    metadata,
    uid(),
    Column("bot_id", BigInteger, nullable=False),
    Column("update_id", BigInteger, nullable=False),
    Column("chat_id", Integer, ForeignKey("chats.id")),
    js("payload"),
    Column("state", String(20), nullable=False, server_default="pending"),
    Column("reason", String(80)),
    timestamp(),
    UniqueConstraint("bot_id", "update_id"),
)
cursors = Table(
    "polling_cursors",
    metadata,
    Column("bot_id", BigInteger, primary_key=True),
    Column("next_offset", BigInteger, nullable=False),
)
jobs = Table(
    "jobs",
    metadata,
    uid(),
    Column("chat_id", Integer, ForeignKey("chats.id")),
    Column("kind", String(32), nullable=False),
    Column("key", String(220), unique=True, nullable=False),
    js("payload"),
    Column("priority", Integer, nullable=False, server_default="10"),
    Column("state", String(16), nullable=False, server_default="pending"),
    Column("attempt", Integer, nullable=False, server_default="0"),
    Column("lease_token", Uuid),
    timestamp("lease_until", nullable=True),
    timestamp("due_at"),
    timestamp(),
    Column("last_error", String(100)),
)
Index("ix_jobs_claim", jobs.c.state, jobs.c.due_at, jobs.c.priority, jobs.c.chat_id)
actions = Table(
    "telegram_actions",
    metadata,
    uid(),
    group(),
    Column("incident_id", Uuid),
    Column("user_id", Integer, ForeignKey("users.id")),
    Column("kind", String(20), nullable=False),
    js("payload"),
    Column("key", String(220), unique=True, nullable=False),
    Column("actor_id", BigInteger),
    Column("binding_version", Integer, nullable=False),
    Column("object_version", Integer),
    Column("policy_version", Integer, nullable=False),
    Column("state", String(16), nullable=False, server_default="planned"),
    Column("error", String(100)),
    js("before"),
    js("result"),
    timestamp(),
    timestamp("updated_at"),
    ForeignKeyConstraint(
        ["chat_id", "incident_id"], ["incidents.chat_id", "incidents.id"]
    ),
)
outbox = Table(
    "outbox",
    metadata,
    uid(),
    Column("chat_id", Integer, ForeignKey("chats.id")),
    Column("destination", BigInteger, nullable=False),
    Column("binding_version", Integer),
    Column("kind", String(24), nullable=False),
    js("payload"),
    Column("key", String(220), unique=True, nullable=False),
    Column("state", String(16), nullable=False, server_default="pending"),
    Column("telegram_message_id", BigInteger),
    timestamp(),
)
cards = Table(
    "admin_cards",
    metadata,
    uid(),
    group(),
    Column("incident_id", Uuid, nullable=False),
    Column("admin_chat_id", BigInteger, nullable=False),
    Column("message_id", BigInteger, nullable=False),
    Column("binding_version", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    UniqueConstraint("admin_chat_id", "message_id"),
    ForeignKeyConstraint(
        ["chat_id", "incident_id"], ["incidents.chat_id", "incidents.id"]
    ),
)
sessions = Table(
    "ui_sessions",
    metadata,
    Column("actor_id", BigInteger, primary_key=True),
    Column("admin_chat_id", BigInteger, primary_key=True),
    Column("chat_id", Integer, ForeignKey("chats.id")),
    js("state"),
    timestamp("expires_at"),
)
operations = Table(
    "ui_operations",
    metadata,
    Column("token", String(40), primary_key=True),
    Column("actor_id", BigInteger, nullable=False),
    Column("admin_chat_id", BigInteger, nullable=False),
    group(),
    Column("binding_version", Integer, nullable=False),
    Column("object_version", Integer),
    Column("kind", String(32), nullable=False),
    js("payload"),
    timestamp("expires_at"),
    timestamp("used_at", nullable=True),
    js("result"),
)
onboarding = Table(
    "onboarding_requests",
    metadata,
    uid(),
    Column("owner_id", BigInteger, nullable=False),
    Column("manager_id", BigInteger, nullable=False),
    Column("work_actor_id", BigInteger, nullable=False),
    Column("admin_actor_id", BigInteger, nullable=False),
    Column("work_chat_id", BigInteger),
    Column("admin_chat_id", BigInteger),
    Column("work_confirmed", Boolean, nullable=False, server_default="false"),
    Column("admin_confirmed", Boolean, nullable=False, server_default="false"),
    Column("state", String(20), nullable=False, server_default="pending"),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("durable", Boolean, nullable=False, server_default="false"),
    Column("approval_required", Boolean, nullable=False, server_default="false"),
    Column("approved_by", BigInteger),
    Column("approved_version", Integer),
    Column("review_note", Text),
    js("initial_policy"),
    timestamp("expires_at"),
    timestamp(),
)
selections = Table(
    "onboarding_selections",
    metadata,
    Column("request_id", Integer, primary_key=True),
    Column("request", Uuid, ForeignKey("onboarding_requests.id"), nullable=False),
    Column("actor_id", BigInteger, nullable=False),
    Column("side", String(8), nullable=False),
    Column("token_hash", String(64)),
    Column("attempts", Integer, nullable=False, server_default="0"),
    timestamp("expires_at"),
    timestamp("used_at", nullable=True),
)
candidates = Table(
    "onboarding_candidates",
    metadata,
    Column("telegram_chat_id", BigInteger, primary_key=True),
    Column("inviter_id", BigInteger),
    Column("title", Text),
    timestamp("updated_at"),
)
audit = Table(
    "audit_events",
    metadata,
    uid(),
    Column("chat_id", Integer, ForeignKey("chats.id")),
    Column("actor_id", BigInteger),
    Column("kind", String(64), nullable=False),
    js("details"),
    timestamp(),
)
migrations = Table(
    "migration_runs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("state", String(20), nullable=False),
    js("manifest"),
    js("report"),
    timestamp(),
    timestamp("finished_at", nullable=True),
)
budget = Table(
    "budget_reservations",
    metadata,
    uid(),
    group(),
    Column("day", String(10), nullable=False),
    Column("key", String(220), nullable=False, unique=True),
    Column("reserved", Float, nullable=False),
    Column("spent", Float),
    Column("state", String(16), nullable=False, server_default="reserved"),
    timestamp(),
)

for table, cols in [
    (items, ["chat_id", "telegram_date", "id"]),
    (items, ["chat_id", "user_id", "telegram_date", "id"]),
    (items, ["chat_id", "incident_key", "telegram_message_id"]),
    (incidents, ["chat_id", "status", "created_at", "id"]),
    (checks, ["chat_id", "created_at", "id"]),
    (audit, ["chat_id", "created_at", "id"]),
    (credits, ["chat_id", "user_id", "cycle"]),
    (actions, ["incident_id", "created_at"]),
    (cards, ["chat_id", "incident_id"]),
]:
    Index("ix_" + table.name + "_" + "_".join(cols), *(table.c[n] for n in cols))

Index(
    "ix_active_admin_chat",
    bindings.c.admin_chat_id,
    postgresql_where=bindings.c.closed_at.is_(None),
)

system = Table(
    "system_state",
    metadata,
    Column("key", String(64), primary_key=True),
    js("value"),
    timestamp("updated_at"),
)

NEW_TABLES = [
    table for table in metadata.sorted_tables if table.name not in legacy_names
]

admin_contacts = Table(
    "admin_contacts",
    metadata,
    Column("admin_chat_id", BigInteger, primary_key=True),
    Column("actor_id", BigInteger, primary_key=True),
    Column("name", Text, nullable=False),
    Column("username", Text),
    timestamp("updated_at"),
)
team_invitations = Table(
    "team_invitations",
    metadata,
    uid(),
    Column("token_hash", String(64), unique=True, nullable=False),
    group(),
    Column("admin_chat_id", BigInteger, nullable=False),
    Column("binding_version", Integer, nullable=False),
    Column("inviter_id", BigInteger, nullable=False),
    Column("candidate_id", BigInteger),
    timestamp("expires_at"),
    timestamp(),
)

group_requests = Table(
    "group_requests",
    metadata,
    uid(),
    group(),
    Column("actor_id", BigInteger, nullable=False),
    Column("kind", String(24), nullable=False),
    Column("state", String(24), nullable=False, server_default="open"),
    Column("resolved_by", BigInteger),
    timestamp(),
    Column("resolved_at", DateTime(timezone=True)),
)
