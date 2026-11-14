"""Durable manager requests to the bot owner; no legacy rows are modified."""

from alembic import op
import sqlalchemy as sa

revision = "0007_group_requests"
down_revision = "0006_team_invitations"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "group_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("chat_id", sa.Integer(), sa.ForeignKey("chats.id"), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="open"),
        sa.Column("resolved_by", sa.BigInteger()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "uq_open_group_request",
        "group_requests",
        ["chat_id", "kind"],
        unique=True,
        postgresql_where=sa.text("state = 'open'"),
    )


def downgrade():
    raise RuntimeError(
        "Use an explicit backup/restore rollback plan; requests must be preserved."
    )
