"""Known admin-chat contacts and scoped invitation candidates."""

from alembic import op
import sqlalchemy as sa

revision = "0006_team_invitations"
down_revision = "0005_onboarding_review"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "admin_contacts",
        sa.Column("admin_chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("actor_id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("username", sa.Text()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "team_invitations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("chat_id", sa.Integer(), sa.ForeignKey("chats.id"), nullable=False),
        sa.Column("admin_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("binding_version", sa.Integer(), nullable=False),
        sa.Column("inviter_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade():
    op.drop_table("team_invitations")
    op.drop_table("admin_contacts")
